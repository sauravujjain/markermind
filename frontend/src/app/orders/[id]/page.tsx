'use client'

import { useEffect, useState } from 'react'
import { useRouter, useParams } from 'next/navigation'
import Link from 'next/link'
import { useAuthStore } from '@/lib/auth-store'
import { api, Order, Pattern, NestingJob, Cutplan, Fabric, PatternPiece } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { ArrowLeft, Play, FileText, CheckCircle2, Clock, Package, ChevronDown, ChevronRight, Settings, AlertCircle, Loader2, Pencil, Eye, Layers, FlipHorizontal, Upload, Plus, XCircle } from 'lucide-react'
import { useRef } from 'react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export default function OrderDetailPage() {
  const router = useRouter()
  const params = useParams()
  const orderId = params.id as string
  const { isAuthenticated, isLoading: authLoading, checkAuth } = useAuthStore()
  const { toast } = useToast()

  const [order, setOrder] = useState<Order | null>(null)
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [fabrics, setFabrics] = useState<Fabric[]>([])
  const [nestingJobs, setNestingJobs] = useState<NestingJob[]>([])
  const [cutplans, setCutplans] = useState<Cutplan[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSavingMappings, setIsSavingMappings] = useState(false)
  const [selectedSizes, setSelectedSizes] = useState<string[]>([])
  const [materialMappings, setMaterialMappings] = useState<Record<string, string>>({})
  const [patternPieces, setPatternPieces] = useState<Record<string, PatternPiece[]>>({})
  const [showPieces, setShowPieces] = useState(false)
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
  const [nestingConfig, setNestingConfig] = useState({
    fabricWidthInches: 60,
    maxBundleCount: 6,
    topNResults: 10,
    fullCoverage: false,
  })
  // New state for redesigned UI
  const [activeFabricTab, setActiveFabricTab] = useState<string>('')
  const [activeNestingFabric, setActiveNestingFabric] = useState<string>('')
  const [perFabricConfig, setPerFabricConfig] = useState<Record<string, {
    widthInches: number
    maxBundles: number
    maxMarkerLengthYards: number
    topN: number
    fullCoverage: boolean
  }>>({})
  const [editablePieces, setEditablePieces] = useState<Record<string, Record<string, { qty: number; leftQty: number; rightQty: number }>>>({})
  const [isPiecesEditMode, setIsPiecesEditMode] = useState(false)

  // Pattern upload state
  const [isUploadingPattern, setIsUploadingPattern] = useState(false)
  const [showPatternUpload, setShowPatternUpload] = useState(false)
  const [patternUploadName, setPatternUploadName] = useState('')
  const dxfInputRef = useRef<HTMLInputElement>(null)
  const rulInputRef = useRef<HTMLInputElement>(null)
  const [selectedDxfFile, setSelectedDxfFile] = useState<File | null>(null)
  const [selectedRulFile, setSelectedRulFile] = useState<File | null>(null)

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.push('/login')
    }
  }, [authLoading, isAuthenticated, router])

  useEffect(() => {
    if (isAuthenticated && orderId) {
      loadData()
    }
  }, [isAuthenticated, orderId])

  // Poll for nesting job progress when any job is running
  useEffect(() => {
    const hasRunningJob = nestingJobs.some(j => j.status === 'running')
    if (!hasRunningJob) return

    const interval = setInterval(async () => {
      try {
        const jobsData = await api.getNestingJobs(orderId)
        setNestingJobs(jobsData)
        // If job just completed, reload everything to update order status
        if (jobsData.every(j => j.status !== 'running') && jobsData.some(j => j.status === 'completed')) {
          loadData()
        }
      } catch (e) {
        console.error('Polling error:', e)
      }
    }, 3000)

    return () => clearInterval(interval)
  }, [nestingJobs.some(j => j.status === 'running'), orderId])

  const loadData = async () => {
    try {
      const [orderData, patternsData, fabricsData, jobsData, cutplansData] = await Promise.all([
        api.getOrder(orderId),
        api.getPatterns(),
        api.getFabrics(),
        api.getNestingJobs(orderId),
        api.getCutplans(orderId),
      ])
      setOrder(orderData)
      setPatterns(patternsData)
      setFabrics(fabricsData)
      setNestingJobs(jobsData)
      setCutplans(cutplansData)

      // Initialize selected sizes and material mappings based on pattern
      if (orderData.pattern_id) {
        const pattern = patternsData.find(p => p.id === orderData.pattern_id)
        if (pattern) {
          // Get order sizes
          const orderSizes = new Set<string>()
          orderData.order_lines.forEach(line => {
            line.size_quantities.forEach(sq => orderSizes.add(sq.size_code))
          })
          // Select pattern sizes that match order sizes
          const matchingSizes = pattern.available_sizes.filter(s => orderSizes.has(s))
          setSelectedSizes(matchingSizes.length > 0 ? matchingSizes : pattern.available_sizes)

          // Initialize material mappings from pattern's fabric_mappings
          const mappings: Record<string, string> = {}
          pattern.fabric_mappings?.forEach(m => {
            if (m.fabric_id) mappings[m.material_name] = m.fabric_id
          })
          setMaterialMappings(mappings)

          // Load pattern pieces
          try {
            const piecesData = await api.getPatternPieces(orderData.pattern_id)
            setPatternPieces(piecesData.pieces_by_material || {})

            // Initialize editable pieces from pattern pieces
            const editables: Record<string, Record<string, { qty: number; leftQty: number; rightQty: number }>> = {}
            Object.entries(piecesData.pieces_by_material || {}).forEach(([material, pieces]) => {
              editables[material] = {}
              pieces.forEach((piece: PatternPiece) => {
                editables[material][piece.name] = {
                  qty: piece.quantity,
                  leftQty: piece.left_qty,
                  rightQty: piece.right_qty,
                }
              })
            })
            setEditablePieces(editables)
          } catch (e) {
            // Pattern might not be parsed yet
            console.log('Could not load pattern pieces:', e)
          }

          // Get order fabric codes
          const orderFabricCodes = Array.from(new Set(orderData.order_lines.map(line => line.fabric_code)))

          // Set active fabric tabs
          if (orderFabricCodes.length > 0) {
            setActiveFabricTab(orderFabricCodes[0])
            // Find first fabric that exists in pattern
            const firstPatternFabric = orderFabricCodes.find(code => pattern.available_materials.includes(code))
            setActiveNestingFabric(firstPatternFabric || orderFabricCodes[0])
          }

          // Initialize per-fabric nesting config
          const fabricConfigs: Record<string, { widthInches: number; maxBundles: number; maxMarkerLengthYards: number; topN: number; fullCoverage: boolean }> = {}
          orderFabricCodes.forEach(code => {
            const fabric = fabricsData.find(f => f.code === code)
            fabricConfigs[code] = {
              widthInches: fabric?.width_inches || 60,
              maxBundles: 6,
              maxMarkerLengthYards: 15,
              topN: 10,
              fullCoverage: false,
            }
          })
          setPerFabricConfig(fabricConfigs)

          // Initialize nesting config from first fabric
          if (orderFabricCodes.length > 0) {
            const matchedFabric = fabricsData.find(f => f.code === orderFabricCodes[0])
            if (matchedFabric) {
              setNestingConfig(prev => ({
                ...prev,
                fabricWidthInches: matchedFabric.width_inches || 60,
              }))
            }
          }
        }
      }
    } catch (error) {
      toast({
        title: 'Failed to load order',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  // Helper to get auto-matched mappings
  const getAutoMatchedMappings = () => {
    if (!order?.pattern_id) return {}
    const pattern = patterns.find(p => p.id === order.pattern_id)
    if (!pattern) return {}

    const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
    const autoMatched: Record<string, string> = {}

    pattern.available_materials.forEach(material => {
      if (orderFabricCodes.includes(material)) {
        const fabric = fabrics.find(f => f.code === material)
        if (fabric) {
          autoMatched[material] = fabric.id
        }
      }
    })
    return autoMatched
  }

  // Handle pattern upload
  const handlePatternUpload = async () => {
    if (!selectedDxfFile || !patternUploadName.trim()) {
      toast({
        title: 'Missing required fields',
        description: 'Please provide a pattern name and DXF file',
        variant: 'destructive',
      })
      return
    }

    setIsUploadingPattern(true)
    try {
      const newPattern = await api.uploadPattern(
        patternUploadName.trim(),
        'aama',
        selectedDxfFile,
        selectedRulFile || undefined
      )

      toast({ title: 'Pattern uploaded successfully' })

      // Auto-select the new pattern
      await api.updateOrder(orderId, { pattern_id: newPattern.id })

      // Reset upload form
      setShowPatternUpload(false)
      setPatternUploadName('')
      setSelectedDxfFile(null)
      setSelectedRulFile(null)

      // Reload data to refresh patterns list
      loadData()
    } catch (error) {
      toast({
        title: 'Failed to upload pattern',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsUploadingPattern(false)
    }
  }

  const handleSaveMappings = async () => {
    if (!order?.pattern_id) return

    const pattern = patterns.find(p => p.id === order.pattern_id)
    if (!pattern) return

    // Combine auto-matched with manual selections
    const autoMatched = getAutoMatchedMappings()
    const effectiveMappings = { ...autoMatched, ...materialMappings }

    setIsSavingMappings(true)
    try {
      const mappings = Object.entries(effectiveMappings)
        .filter(([_, fabricId]) => fabricId)
        .map(([material_name, fabric_id]) => ({
          material_name,
          fabric_id,
        }))

      await api.updateFabricMappings(order.pattern_id, mappings)
      toast({ title: 'Configuration saved' })
      loadData()
    } catch (error) {
      toast({
        title: 'Failed to save configuration',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsSavingMappings(false)
    }
  }

  const handleStartNesting = async () => {
    if (!order?.pattern_id) {
      toast({
        title: 'No pattern selected',
        description: 'Please select a pattern before starting nesting',
        variant: 'destructive',
      })
      return
    }

    const pattern = patterns.find(p => p.id === order.pattern_id)
    if (!pattern) {
      toast({
        title: 'Pattern not found',
        description: 'Please select a valid pattern',
        variant: 'destructive',
      })
      return
    }

    // Get order fabric codes and check if they're configured
    const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
    const orderFabricsInPattern = orderFabricCodes.filter(code =>
      pattern.available_materials.includes(code)
    )

    // Check if all order fabrics have fabric records
    const autoMatched = getAutoMatchedMappings()
    const effectiveMappings = { ...autoMatched, ...materialMappings }

    const allConfigured = orderFabricsInPattern.every(code => {
      const fabric = fabrics.find(f => f.code === code)
      return fabric && effectiveMappings[code]
    })

    if (!allConfigured) {
      toast({
        title: 'Configuration required',
        description: 'Please configure fabric records for all order fabrics',
        variant: 'destructive',
      })
      return
    }

    try {
      // Use perFabricConfig if available for the active nesting fabric
      const fabricConfig = activeNestingFabric ? perFabricConfig[activeNestingFabric] : undefined
      const job = await api.createNestingJob({
        order_id: orderId,
        pattern_id: order.pattern_id,
        fabric_width_inches: fabricConfig?.widthInches || nestingConfig.fabricWidthInches,
        max_bundle_count: fabricConfig?.maxBundles || nestingConfig.maxBundleCount,
        top_n_results: fabricConfig?.topN || nestingConfig.topNResults,
        full_coverage: fabricConfig?.fullCoverage || nestingConfig.fullCoverage,
      })
      toast({
        title: 'Nesting job started',
        description: 'The GPU nesting job has been queued',
      })
      loadData() // Reload to show new job
    } catch (error) {
      toast({
        title: 'Failed to start nesting',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    }
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
      // Start polling for status
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

  const cutplanPollingRef = useRef<NodeJS.Timeout | null>(null)
  const lastStrategiesDone = useRef(0)

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
          const plans = await api.getCutplans(orderId)
          setCutplans(plans)
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

  const handleCancelCutplan = async () => {
    try {
      await api.cancelCutplanOptimize(orderId)
      toast({ title: 'Cancellation requested', description: 'The solver will stop after the current strategy' })
    } catch (error) {
      toast({ title: 'Failed to cancel', description: error instanceof Error ? error.message : 'Please try again', variant: 'destructive' })
    }
  }

  // Cleanup cutplan polling on unmount
  useEffect(() => {
    return () => { if (cutplanPollingRef.current) clearInterval(cutplanPollingRef.current) }
  }, [])

  // Check if cutplan optimization is already running on load
  useEffect(() => {
    if (!isAuthenticated || !orderId) return
    api.getCutplanOptimizeStatus(orderId).then(status => {
      if (status.status === 'running') {
        setCutplanOptStatus(status)
        setIsGeneratingCutplan(true)
        startCutplanPolling()
      }
    }).catch(() => {})
  }, [isAuthenticated, orderId])

  if (authLoading || !isAuthenticated || isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  if (!order) {
    return (
      <DashboardLayout>
        <div className="text-center py-12">
          <h2 className="text-xl font-semibold">Order not found</h2>
          <Link href="/orders">
            <Button className="mt-4">Back to Orders</Button>
          </Link>
        </div>
      </DashboardLayout>
    )
  }

  const totalUnits = order.order_lines.reduce(
    (acc, c) => acc + c.size_quantities.reduce((a, s) => a + s.quantity, 0),
    0
  )

  return (
    <DashboardLayout>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <Link href="/orders">
              <Button variant="ghost" size="icon">
                <ArrowLeft className="h-4 w-4" />
              </Button>
            </Link>
            <div>
              <h1 className="text-3xl font-bold tracking-tight">{order.order_number}</h1>
              <p className="text-muted-foreground">
                {order.order_lines.length} colors, {totalUnits} total units
              </p>
            </div>
          </div>
          {(() => {
            const pattern = order.pattern_id ? patterns.find(p => p.id === order.pattern_id) : null
            const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
            const orderFabricsInPattern = pattern
              ? orderFabricCodes.filter(code => pattern.available_materials.includes(code))
              : []
            const allConfigured = orderFabricsInPattern.every(code => {
              const fabric = fabrics.find(f => f.code === code)
              return !!fabric
            })
            const isConfigured = pattern && orderFabricsInPattern.length > 0 && allConfigured
            const hasNestingResults = nestingJobs.some(j => j.status === 'completed')

            return (
              <div className="flex space-x-2">
                <Button
                  variant="outline"
                  onClick={handleStartNesting}
                  disabled={!order.pattern_id || !isConfigured}
                  title={!order.pattern_id ? 'Select a pattern first' : !isConfigured ? 'Configure fabrics first' : 'Start GPU nesting'}
                >
                  <Play className="mr-2 h-4 w-4" />
                  Run Nesting
                </Button>
                <Button
                  onClick={() => {
                    setShowCutplanConfig(true)
                    setTimeout(() => document.getElementById('cutplan-config-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
                  }}
                  disabled={!hasNestingResults}
                  title={!hasNestingResults ? 'Run nesting first' : 'Generate cutplan options'}
                >
                  Generate Cutplan
                </Button>
              </div>
            )
          })()}
        </div>

        {/* Workflow Steps */}
        {(() => {
          const pattern = order.pattern_id ? patterns.find(p => p.id === order.pattern_id) : null
          const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
          const orderFabricsInPattern = pattern
            ? orderFabricCodes.filter(code => pattern.available_materials.includes(code))
            : []
          const allConfigured = orderFabricsInPattern.every(code => {
            const fabric = fabrics.find(f => f.code === code)
            return !!fabric
          })
          const isConfigured = pattern && orderFabricsInPattern.length > 0 && allConfigured
          return (
            <div className="grid gap-3 md:grid-cols-6">
              <Card className={order.order_lines.length > 0 ? 'border-green-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${order.order_lines.length > 0 ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                      {order.order_lines.length > 0 ? <CheckCircle2 className="h-3.5 w-3.5" /> : '1'}
                    </div>
                    <CardTitle className="text-xs">Order</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {order.order_lines.length > 0 ? `${order.order_lines.length} colors` : 'Add quantities'}
                  </p>
                </CardContent>
              </Card>

              <Card className={order.pattern_id ? 'border-green-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${order.pattern_id ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                      {order.pattern_id ? <CheckCircle2 className="h-3.5 w-3.5" /> : '2'}
                    </div>
                    <CardTitle className="text-xs">Pattern</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {order.pattern_id ? 'Selected' : 'Choose pattern'}
                  </p>
                </CardContent>
              </Card>

              <Card className={isConfigured ? 'border-green-500' : order.pattern_id ? 'border-amber-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${isConfigured ? 'bg-green-100 text-green-600' : order.pattern_id ? 'bg-amber-100 text-amber-600' : 'bg-gray-100 text-gray-400'}`}>
                      {isConfigured ? <CheckCircle2 className="h-3.5 w-3.5" /> : order.pattern_id ? <Settings className="h-3.5 w-3.5" /> : '3'}
                    </div>
                    <CardTitle className="text-xs">Configure</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {isConfigured ? 'Ready' : order.pattern_id ? 'Map fabrics' : 'Pending'}
                  </p>
                </CardContent>
              </Card>

              <Card className={nestingJobs.some(j => j.status === 'completed') ? 'border-green-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${nestingJobs.some(j => j.status === 'completed') ? 'bg-green-100 text-green-600' : nestingJobs.some(j => j.status === 'running') ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-400'}`}>
                      {nestingJobs.some(j => j.status === 'completed') ? <CheckCircle2 className="h-3.5 w-3.5" /> : nestingJobs.some(j => j.status === 'running') ? <Clock className="h-3.5 w-3.5 animate-pulse" /> : '4'}
                    </div>
                    <CardTitle className="text-xs">Nesting</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {nestingJobs.some(j => j.status === 'completed') ? 'Complete' : nestingJobs.some(j => j.status === 'running') ? 'Running...' : 'GPU nesting'}
                  </p>
                </CardContent>
              </Card>

              <Card className={cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? 'border-green-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                      {cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? <CheckCircle2 className="h-3.5 w-3.5" /> : '5'}
                    </div>
                    <CardTitle className="text-xs">Cutplan</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {cutplans.some(c => c.status === 'approved') ? 'Approved' : cutplans.length > 0 ? 'Review' : 'Optimize'}
                  </p>
                </CardContent>
              </Card>

              <Card className={order.status === 'completed' ? 'border-green-500' : ''}>
                <CardHeader className="pb-2">
                  <div className="flex items-center space-x-2">
                    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${order.status === 'completed' ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                      {order.status === 'completed' ? <CheckCircle2 className="h-3.5 w-3.5" /> : '6'}
                    </div>
                    <CardTitle className="text-xs">Export</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground">
                    {order.status === 'completed' ? 'Done' : 'Dockets'}
                  </p>
                </CardContent>
              </Card>
            </div>
          )
        })()}

        {/* Order Details */}
        <div className="grid gap-6 md:grid-cols-2">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Order Summary</CardTitle>
                  <CardDescription>
                    {(() => {
                      const uniqueColors = new Set(order.order_lines.map(l => l.color_code)).size
                      const uniqueFabrics = new Set(order.order_lines.map(l => l.fabric_code)).size
                      // Get per-fabric total (same qty for all fabrics)
                      const firstFabricLines = order.order_lines.filter(l => l.fabric_code === order.order_lines[0]?.fabric_code)
                      const perFabricUnits = firstFabricLines.reduce((acc, line) =>
                        acc + line.size_quantities.reduce((a, sq) => a + sq.quantity, 0), 0)
                      return `${perFabricUnits.toLocaleString()} units/fabric • ${uniqueColors} colors • ${uniqueFabrics} fabrics`
                    })()}
                  </CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {order.order_lines.length === 0 ? (
                <p className="text-muted-foreground">No quantities added yet</p>
              ) : (
                <div className="space-y-3">
                  {/* Fabric tabs */}
                  {(() => {
                    const orderFabricCodes = Array.from(new Set(order.order_lines.map(l => l.fabric_code)))
                    const allSizes = Array.from(new Set(
                      order.order_lines.flatMap(line => line.size_quantities.map(sq => sq.size_code))
                    )).sort()

                    // Get colors for first fabric (same for all fabrics)
                    const firstFabricLines = order.order_lines.filter(l => l.fabric_code === orderFabricCodes[0])

                    return (
                      <div className="space-y-3">
                        {/* Fabric tabs */}
                        <div className="flex gap-1 p-1 bg-muted/50 rounded-lg">
                          {orderFabricCodes.map((fabricCode) => {
                            const fabric = fabrics.find(f => f.code === fabricCode)
                            return (
                              <button
                                key={fabricCode}
                                onClick={() => setActiveFabricTab(fabricCode)}
                                className={`flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-all ${
                                  activeFabricTab === fabricCode
                                    ? 'bg-background shadow-sm text-foreground'
                                    : 'text-muted-foreground hover:text-foreground'
                                }`}
                              >
                                {fabricCode}
                              </button>
                            )
                          })}
                        </div>

                        {/* Color breakdown table */}
                        <div className="overflow-x-auto">
                          <table className="w-full text-sm">
                            <thead>
                              <tr className="border-b">
                                <th className="text-left py-2 pr-3 font-medium text-muted-foreground">Color</th>
                                {allSizes.map(size => (
                                  <th key={size} className="text-center py-2 px-1.5 font-medium text-muted-foreground min-w-[40px]">
                                    {size}
                                  </th>
                                ))}
                                <th className="text-right py-2 pl-3 font-medium text-muted-foreground">Total</th>
                              </tr>
                            </thead>
                            <tbody>
                              {firstFabricLines.map((line) => {
                                const lineTotal = line.size_quantities.reduce((a, sq) => a + sq.quantity, 0)
                                const qtyMap = Object.fromEntries(line.size_quantities.map(sq => [sq.size_code, sq.quantity]))
                                return (
                                  <tr key={line.id} className="border-b border-border/50 hover:bg-muted/30">
                                    <td className="py-2 pr-3 font-medium">{line.color_code}</td>
                                    {allSizes.map(size => (
                                      <td key={size} className="text-center py-2 px-1.5 tabular-nums">
                                        {qtyMap[size] || <span className="text-muted-foreground/30">-</span>}
                                      </td>
                                    ))}
                                    <td className="text-right py-2 pl-3 font-medium tabular-nums">
                                      {lineTotal.toLocaleString()}
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                            <tfoot>
                              <tr className="bg-muted/50 font-medium">
                                <td className="py-2 pr-3">Total</td>
                                {allSizes.map(size => {
                                  const sizeTotal = firstFabricLines.reduce((acc, line) => {
                                    const sq = line.size_quantities.find(s => s.size_code === size)
                                    return acc + (sq?.quantity || 0)
                                  }, 0)
                                  return (
                                    <td key={size} className="text-center py-2 px-1.5 tabular-nums">
                                      {sizeTotal > 0 ? sizeTotal.toLocaleString() : '-'}
                                    </td>
                                  )
                                })}
                                <td className="text-right py-2 pl-3 tabular-nums">
                                  {firstFabricLines.reduce((acc, line) =>
                                    acc + line.size_quantities.reduce((a, sq) => a + sq.quantity, 0), 0
                                  ).toLocaleString()}
                                </td>
                              </tr>
                            </tfoot>
                          </table>
                        </div>
                      </div>
                    )
                  })()}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Pattern</CardTitle>
                  <CardDescription>Select or upload a pattern for this order</CardDescription>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowPatternUpload(!showPatternUpload)}
                  className="gap-2"
                >
                  <Upload className="h-4 w-4" />
                  Upload New
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {/* Upload Pattern Form */}
                {showPatternUpload && (
                  <div className="p-4 border border-dashed border-primary/30 rounded-lg bg-primary/5 space-y-4">
                    <h4 className="font-medium text-sm flex items-center gap-2">
                      <Plus className="h-4 w-4" />
                      Upload New Pattern
                    </h4>

                    {/* Pattern Name */}
                    <div>
                      <label className="text-xs font-medium text-muted-foreground block mb-1">Pattern Name</label>
                      <input
                        type="text"
                        value={patternUploadName}
                        onChange={(e) => setPatternUploadName(e.target.value)}
                        placeholder="Enter pattern name..."
                        className="w-full px-3 py-2 border rounded-md text-sm"
                      />
                    </div>

                    {/* DXF File */}
                    <div>
                      <label className="text-xs font-medium text-muted-foreground block mb-1">DXF File (required)</label>
                      <input
                        ref={dxfInputRef}
                        type="file"
                        accept=".dxf"
                        onChange={(e) => setSelectedDxfFile(e.target.files?.[0] || null)}
                        className="hidden"
                      />
                      <button
                        type="button"
                        onClick={() => dxfInputRef.current?.click()}
                        className={`w-full px-3 py-2 border rounded-md text-sm text-left flex items-center gap-2 ${
                          selectedDxfFile ? 'bg-green-50 border-green-200' : 'bg-muted/30 hover:bg-muted/50'
                        }`}
                      >
                        <FileText className="h-4 w-4" />
                        {selectedDxfFile ? selectedDxfFile.name : 'Choose DXF file...'}
                      </button>
                    </div>

                    {/* RUL File */}
                    <div>
                      <label className="text-xs font-medium text-muted-foreground block mb-1">RUL File (optional)</label>
                      <input
                        ref={rulInputRef}
                        type="file"
                        accept=".rul"
                        onChange={(e) => setSelectedRulFile(e.target.files?.[0] || null)}
                        className="hidden"
                      />
                      <button
                        type="button"
                        onClick={() => rulInputRef.current?.click()}
                        className={`w-full px-3 py-2 border rounded-md text-sm text-left flex items-center gap-2 ${
                          selectedRulFile ? 'bg-green-50 border-green-200' : 'bg-muted/30 hover:bg-muted/50'
                        }`}
                      >
                        <FileText className="h-4 w-4" />
                        {selectedRulFile ? selectedRulFile.name : 'Choose RUL file (sizes)...'}
                      </button>
                    </div>

                    {/* Upload Actions */}
                    <div className="flex gap-2 pt-2">
                      <Button
                        onClick={handlePatternUpload}
                        disabled={isUploadingPattern || !selectedDxfFile || !patternUploadName.trim()}
                        className="flex-1"
                      >
                        {isUploadingPattern ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Uploading...
                          </>
                        ) : (
                          <>
                            <Upload className="mr-2 h-4 w-4" />
                            Upload & Select
                          </>
                        )}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => {
                          setShowPatternUpload(false)
                          setPatternUploadName('')
                          setSelectedDxfFile(null)
                          setSelectedRulFile(null)
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}

                {/* Pattern Selector */}
                <div className="space-y-2">
                  <label className="text-xs font-medium text-muted-foreground block">Select Existing Pattern</label>
                  <Select
                    value={order.pattern_id || ''}
                    onValueChange={async (value) => {
                      try {
                        await api.updateOrder(orderId, { pattern_id: value || undefined })
                        toast({ title: 'Pattern updated' })
                        loadData()
                      } catch (error) {
                        toast({
                          title: 'Failed to update pattern',
                          variant: 'destructive',
                        })
                      }
                    }}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Select a pattern..." />
                    </SelectTrigger>
                    <SelectContent>
                      {patterns.filter(p => p.is_parsed).map((pattern) => (
                        <SelectItem key={pattern.id} value={pattern.id}>
                          <div className="flex items-center space-x-2">
                            <Package className="h-4 w-4" />
                            <span>{pattern.name}</span>
                            <span className="text-xs text-muted-foreground">
                              ({pattern.available_sizes.length} sizes)
                            </span>
                          </div>
                        </SelectItem>
                      ))}
                      {patterns.filter(p => p.is_parsed).length === 0 && (
                        <SelectItem value="none" disabled>
                          No parsed patterns available
                        </SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </div>

                {order.pattern_id && (
                  <div className="space-y-3">
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-3">
                          <Package className="h-6 w-6 text-primary" />
                          <div>
                            <p className="font-medium text-sm">
                              {patterns.find(p => p.id === order.pattern_id)?.name}
                            </p>
                            <p className="text-xs text-muted-foreground">
                              Sizes: {patterns.find(p => p.id === order.pattern_id)?.available_sizes.join(', ')}
                            </p>
                            <p className="text-xs text-muted-foreground">
                              Materials: {patterns.find(p => p.id === order.pattern_id)?.available_materials.join(', ') || 'None'}
                            </p>
                          </div>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setShowPieces(!showPieces)}
                        >
                          <ChevronDown className={`h-4 w-4 transition-transform ${showPieces ? 'rotate-180' : ''}`} />
                          {showPieces ? 'Hide' : 'Show'} Pieces
                        </Button>
                      </div>
                    </div>

                    {/* Pattern Pieces Display */}
                    {showPieces && Object.keys(patternPieces).length > 0 && (
                      <div className="space-y-4 animate-in slide-in-from-top-2">
                        {Object.entries(patternPieces).map(([material, pieces]) => (
                          <div key={material} className="border rounded-lg p-3">
                            <h5 className="text-sm font-medium mb-2 flex items-center gap-2">
                              <span className="w-3 h-3 rounded-full bg-accent" />
                              {material}
                              <span className="text-xs text-muted-foreground">
                                ({pieces.length} pieces)
                              </span>
                            </h5>
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                              {pieces.map((piece, idx) => (
                                <div
                                  key={`${piece.name}-${idx}`}
                                  className="p-2 bg-muted/30 rounded text-xs"
                                >
                                  <div className="font-medium truncate" title={piece.name}>
                                    {piece.name}
                                  </div>
                                  <div className="text-muted-foreground flex flex-wrap gap-1 mt-1">
                                    <span className="bg-primary/10 text-primary px-1.5 py-0.5 rounded">
                                      x{piece.quantity}
                                    </span>
                                    {piece.has_left_right && (
                                      <span className="bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded">
                                        L/R
                                      </span>
                                    )}
                                    {piece.has_grain_line && (
                                      <span className="bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded">
                                        Grain
                                      </span>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {patterns.filter(p => p.is_parsed).length === 0 && (
                  <div className="text-center py-2">
                    <p className="text-sm text-muted-foreground mb-2">No patterns available</p>
                    <Link href="/patterns">
                      <Button variant="outline" size="sm">
                        Upload Pattern
                      </Button>
                    </Link>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Configure Section - shown after pattern is selected */}
        {order.pattern_id && (() => {
          const pattern = patterns.find(p => p.id === order.pattern_id)
          if (!pattern) return null

          // Get order fabric codes (from order lines)
          const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))

          // Get order sizes
          const orderSizes = new Set<string>()
          order.order_lines.forEach(line => {
            line.size_quantities.forEach(sq => orderSizes.add(sq.size_code))
          })

          // Auto-match: find pattern materials that match order fabric codes
          const autoMatched: Record<string, string> = {}
          pattern.available_materials.forEach(material => {
            // Check if this material matches an order fabric code
            if (orderFabricCodes.includes(material)) {
              // Find the fabric record for this code
              const fabric = fabrics.find(f => f.code === material)
              if (fabric) {
                autoMatched[material] = fabric.id
              }
            }
          })

          // Merge auto-matched with user selections
          const effectiveMappings = { ...autoMatched, ...materialMappings }

          // Get the fabric width from the first mapped fabric
          const mappedFabricId = Object.values(effectiveMappings).find(id => id)
          const mappedFabric = fabrics.find(f => f.id === mappedFabricId)

          // Check if all order fabrics are mapped
          const orderFabricsInPattern = orderFabricCodes.filter(code =>
            pattern.available_materials.includes(code)
          )
          const allOrderFabricsMapped = orderFabricsInPattern.every(code => {
            const fabric = fabrics.find(f => f.code === code)
            return fabric && (effectiveMappings[code] || autoMatched[code])
          })

          const isReadyForNesting = orderFabricsInPattern.length > 0 && allOrderFabricsMapped

          return (
            <Card className="border-accent/30">
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-accent to-accent/70 flex items-center justify-center">
                      <Settings className="h-5 w-5 text-accent-foreground" />
                    </div>
                    <div>
                      <CardTitle>Configure Nesting</CardTitle>
                      <CardDescription>
                        Match order fabrics to pattern materials and set nesting parameters
                      </CardDescription>
                    </div>
                  </div>
                  {isReadyForNesting ? (
                    <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-green-100 text-green-700 text-sm font-medium">
                      <CheckCircle2 className="h-4 w-4" />
                      Ready for Nesting
                    </span>
                  ) : (
                    <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-amber-100 text-amber-700 text-sm font-medium">
                      <AlertCircle className="h-4 w-4" />
                      Configuration Needed
                    </span>
                  )}
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-6">
                  {/* Order Fabrics vs Pattern Materials */}
                  <div className="space-y-4">
                    <div>
                      <h4 className="font-medium text-sm mb-1">Fabric Matching</h4>
                      <p className="text-xs text-muted-foreground">
                        Order has {orderFabricCodes.length} fabric(s): <span className="font-medium">{orderFabricCodes.join(', ')}</span>
                        {' • '}
                        Pattern has {pattern.available_materials.length} material(s): <span className="font-medium">{pattern.available_materials.join(', ')}</span>
                      </p>
                    </div>

                    <div className="grid gap-4 lg:grid-cols-2">
                      {/* Left: Order Fabrics (what we need to nest) */}
                      <div className="space-y-3">
                        <h5 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Order Fabrics → Pattern Materials</h5>
                        {orderFabricCodes.map((fabricCode) => {
                          const inPattern = pattern.available_materials.includes(fabricCode)
                          const fabric = fabrics.find(f => f.code === fabricCode)
                          const isAutoMatched = inPattern && fabric

                          return (
                            <div
                              key={fabricCode}
                              className={`p-3 rounded-lg border ${
                                isAutoMatched
                                  ? 'bg-green-50 border-green-200'
                                  : inPattern
                                  ? 'bg-amber-50 border-amber-200'
                                  : 'bg-red-50 border-red-200'
                              }`}
                            >
                              <div className="flex items-center justify-between">
                                <div className="flex items-center gap-2">
                                  <span className="font-medium">{fabricCode}</span>
                                  {fabric && (
                                    <span className="text-xs text-muted-foreground">
                                      ({fabric.width_inches}" wide)
                                    </span>
                                  )}
                                </div>
                                {isAutoMatched ? (
                                  <span className="flex items-center gap-1 text-xs text-green-700">
                                    <CheckCircle2 className="h-3.5 w-3.5" />
                                    Auto-matched
                                  </span>
                                ) : inPattern ? (
                                  <span className="flex items-center gap-1 text-xs text-amber-700">
                                    <AlertCircle className="h-3.5 w-3.5" />
                                    No fabric record
                                  </span>
                                ) : (
                                  <span className="flex items-center gap-1 text-xs text-red-700">
                                    <AlertCircle className="h-3.5 w-3.5" />
                                    Not in pattern
                                  </span>
                                )}
                              </div>
                              {!fabric && inPattern && (
                                <p className="text-xs text-amber-600 mt-1">
                                  Create a fabric record for "{fabricCode}" in{' '}
                                  <Link href="/settings/fabrics" className="underline">Settings → Fabrics</Link>
                                </p>
                              )}
                            </div>
                          )
                        })}
                      </div>

                      {/* Right: Pattern-only materials (not in order) */}
                      <div className="space-y-3">
                        <h5 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Pattern-only Materials (not in order)</h5>
                        {pattern.available_materials.filter(m => !orderFabricCodes.includes(m)).length === 0 ? (
                          <p className="text-sm text-muted-foreground p-3 bg-muted/30 rounded-lg">
                            All pattern materials are in the order
                          </p>
                        ) : (
                          pattern.available_materials.filter(m => !orderFabricCodes.includes(m)).map((material) => (
                            <div
                              key={material}
                              className="p-3 rounded-lg bg-muted/30 border border-border"
                            >
                              <div className="flex items-center justify-between">
                                <span className="font-medium text-muted-foreground">{material}</span>
                                <span className="text-xs text-muted-foreground">Not needed for this order</span>
                              </div>
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Size Selection */}
                  <div className="space-y-3 pt-4 border-t">
                    <div className="flex items-center justify-between">
                      <div>
                        <h4 className="font-medium text-sm mb-1">Size Selection</h4>
                        <p className="text-xs text-muted-foreground">
                          Pattern: {pattern.available_sizes.length} sizes • Selected: {selectedSizes.length} sizes
                        </p>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => {
                            // Select all sizes that are in the order
                            const orderSizesList = Array.from(orderSizes)
                            setSelectedSizes(pattern.available_sizes.filter(s => orderSizesList.includes(s)))
                          }}
                          className="text-xs text-primary hover:underline"
                        >
                          Select Order Sizes
                        </button>
                        <button
                          onClick={() => setSelectedSizes([...pattern.available_sizes])}
                          className="text-xs text-primary hover:underline"
                        >
                          Select All
                        </button>
                        <button
                          onClick={() => setSelectedSizes([])}
                          className="text-xs text-muted-foreground hover:underline"
                        >
                          Clear
                        </button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {pattern.available_sizes.map((size) => {
                        const inOrder = orderSizes.has(size)
                        const isSelected = selectedSizes.includes(size)
                        return (
                          <button
                            key={size}
                            onClick={() => {
                              if (isSelected) {
                                setSelectedSizes(selectedSizes.filter(s => s !== size))
                              } else {
                                setSelectedSizes([...selectedSizes, size])
                              }
                            }}
                            className={`px-2.5 py-1 rounded-md text-xs font-medium transition-all ${
                              isSelected
                                ? 'bg-green-100 text-green-700 border border-green-300 ring-1 ring-green-300'
                                : inOrder
                                ? 'bg-muted hover:bg-muted/80 text-foreground border border-border'
                                : 'bg-muted/50 text-muted-foreground border border-border/50 hover:bg-muted'
                            }`}
                          >
                            {size}
                            {!inOrder && !isSelected && <span className="ml-1 opacity-60">(extra)</span>}
                          </button>
                        )
                      })}
                      {Array.from(orderSizes).filter(s => !pattern.available_sizes.includes(s)).map((size) => (
                        <div
                          key={size}
                          className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-100 text-red-700 border border-red-200"
                        >
                          {size} (missing in pattern!)
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Nesting Preview - Tabbed by Fabric */}
                  <div className="space-y-4 pt-4 border-t">
                    <div>
                      <h4 className="font-medium text-sm">Nesting Preview</h4>
                      <p className="text-xs text-muted-foreground">
                        Select a fabric to configure and run nesting
                      </p>
                    </div>

                    {/* Fabric Selection Tabs (Horizontal) */}
                    <div className="flex gap-1 p-1 bg-muted/50 rounded-lg overflow-x-auto">
                      {orderFabricsInPattern.map((materialCode) => {
                        const pieces = patternPieces[materialCode] || []
                        const isActive = activeNestingFabric === materialCode
                        const lrCount = pieces.filter(p => p.has_left_right).length
                        const totalPcs = pieces.reduce((sum, p) => sum + p.quantity, 0)

                        return (
                          <button
                            key={materialCode}
                            onClick={() => setActiveNestingFabric(materialCode)}
                            className={`flex-1 min-w-[120px] px-4 py-2.5 rounded-md transition-all ${
                              isActive
                                ? 'bg-background shadow-sm ring-1 ring-primary/20'
                                : 'hover:bg-muted/50'
                            }`}
                          >
                            <div className="flex flex-col items-center gap-1">
                              <div className={`font-medium ${isActive ? 'text-primary' : 'text-foreground'}`}>
                                {materialCode}
                              </div>
                              <div className="text-[10px] text-muted-foreground flex items-center gap-1.5">
                                <span>{pieces.length} pcs</span>
                                <span>•</span>
                                <span>{totalPcs}/bndl</span>
                                {lrCount > 0 && (
                                  <>
                                    <span>•</span>
                                    <span className="flex items-center gap-0.5">
                                      <FlipHorizontal className="h-2.5 w-2.5" />
                                      {lrCount}
                                    </span>
                                  </>
                                )}
                              </div>
                            </div>
                          </button>
                        )
                      })}
                    </div>

                    {/* Active Fabric Content */}
                    {activeNestingFabric && (() => {
                      const fabric = fabrics.find(f => f.code === activeNestingFabric)
                      const pieces = patternPieces[activeNestingFabric] || []
                      const config = perFabricConfig[activeNestingFabric] || {
                        widthInches: fabric?.width_inches || 60,
                        maxBundles: 6,
                        maxMarkerLengthYards: 15,
                        topN: 10,
                        fullCoverage: false,
                      }
                      const editables = editablePieces[activeNestingFabric] || {}
                      const totalPiecesPerBundle = pieces.reduce((sum, p) => {
                        const edit = editables[p.name]
                        return sum + (edit?.qty ?? p.quantity)
                      }, 0)
                      const lrPieces = pieces.filter(p => p.has_left_right)

                      return (
                        <div className="border rounded-lg overflow-hidden">
                          {/* Fabric Header */}
                          <div className="bg-muted/50 px-4 py-3 flex items-center justify-between">
                            <div className="flex items-center gap-3">
                              <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
                                <Package className="h-5 w-5 text-primary" />
                              </div>
                              <div>
                                <div className="font-medium">{activeNestingFabric}</div>
                                <div className="text-xs text-muted-foreground">
                                  {pieces.length} unique pieces • {totalPiecesPerBundle} pieces/bundle • {selectedSizes.length} sizes
                                </div>
                              </div>
                            </div>
                            {fabric ? (
                              <span className="flex items-center gap-1 text-xs text-green-700 bg-green-100 px-2 py-1 rounded-full">
                                <CheckCircle2 className="h-3 w-3" />
                                Ready
                              </span>
                            ) : (
                              <span className="flex items-center gap-1 text-xs text-red-700 bg-red-100 px-2 py-1 rounded-full">
                                <AlertCircle className="h-3 w-3" />
                                No Fabric Record
                              </span>
                            )}
                          </div>

                          {/* Nesting Parameters for this Fabric */}
                          <div className="px-4 py-3 bg-muted/20 border-b">
                            <div className="grid grid-cols-4 gap-3 text-xs">
                              <div>
                                <label className="text-muted-foreground block mb-1">Fabric Width</label>
                                <div className="flex items-center gap-1">
                                  <input
                                    type="number"
                                    value={config.widthInches}
                                    onChange={(e) => setPerFabricConfig({
                                      ...perFabricConfig,
                                      [activeNestingFabric]: { ...config, widthInches: parseFloat(e.target.value) || 60 }
                                    })}
                                    className="w-16 px-2 py-1 border rounded text-sm"
                                    min={30}
                                    max={120}
                                    step={0.5}
                                  />
                                  <span className="text-muted-foreground">"</span>
                                </div>
                              </div>
                              <div>
                                <label className="text-muted-foreground block mb-1">Max Bundles</label>
                                <input
                                  type="number"
                                  value={config.maxBundles}
                                  onChange={(e) => setPerFabricConfig({
                                    ...perFabricConfig,
                                    [activeNestingFabric]: { ...config, maxBundles: parseInt(e.target.value) || 6 }
                                  })}
                                  className="w-16 px-2 py-1 border rounded text-sm"
                                  min={1}
                                  max={10}
                                />
                              </div>
                              <div>
                                <label className={`block mb-1 ${config.maxBundles ? 'text-muted-foreground/50' : 'text-muted-foreground'}`}>
                                  Max Length {config.maxBundles ? '(optional)' : ''}
                                </label>
                                <div className="flex items-center gap-1">
                                  <input
                                    type="number"
                                    value={config.maxMarkerLengthYards}
                                    onChange={(e) => setPerFabricConfig({
                                      ...perFabricConfig,
                                      [activeNestingFabric]: { ...config, maxMarkerLengthYards: parseFloat(e.target.value) || 15 }
                                    })}
                                    className={`w-16 px-2 py-1 border rounded text-sm ${config.maxBundles ? 'bg-muted/50 text-muted-foreground' : ''}`}
                                    min={5}
                                    max={30}
                                    step={0.5}
                                    disabled={!!config.maxBundles}
                                  />
                                  <span className="text-muted-foreground">yd</span>
                                </div>
                              </div>
                            </div>
                            {/* 100% Coverage Toggle */}
                            <div className="mt-3 flex items-center gap-2">
                              <label className="flex items-center gap-2 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={config.fullCoverage}
                                  onChange={(e) => setPerFabricConfig({
                                    ...perFabricConfig,
                                    [activeNestingFabric]: { ...config, fullCoverage: e.target.checked }
                                  })}
                                  className="w-4 h-4 rounded border-gray-300 text-primary focus:ring-primary"
                                />
                                <span className="text-xs font-medium">100% Coverage</span>
                              </label>
                              <span className="text-xs text-muted-foreground">(Evaluate all ratio combinations - slower but thorough)</span>
                            </div>
                          </div>

                          {/* Pieces Section - Compact Table */}
                          <div className="px-4 py-3">
                            {/* Compact Summary Header */}
                            <div className="flex items-center justify-between mb-3">
                              <div className="flex items-center gap-4 text-xs">
                                <span><strong className="text-primary">{pieces.length}</strong> pieces</span>
                                <span><strong>{totalPiecesPerBundle}</strong>/bundle</span>
                                <span className="text-amber-600"><strong>{lrPieces.length}</strong> mirrored</span>
                                <span className="text-blue-600"><strong>{selectedSizes.length}</strong> sizes</span>
                              </div>
                            </div>

                            {/* Pieces Table */}
                            <div className="border rounded-lg overflow-hidden">
                              <div className="max-h-[200px] overflow-y-auto">
                                <table className="w-full text-xs">
                                  <thead className="sticky top-0 bg-muted/80 backdrop-blur">
                                    <tr className="border-b">
                                      <th className="text-left py-1.5 px-2 font-medium w-8"></th>
                                      <th className="text-left py-1.5 px-2 font-medium">Piece Name</th>
                                      <th className="text-center py-1.5 px-2 font-medium w-12">Qty</th>
                                      <th className="text-center py-1.5 px-2 font-medium w-16">L/R</th>
                                      <th className="text-center py-1.5 px-2 font-medium w-10"></th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {pieces.map((piece, idx) => {
                                      const edit = editables[piece.name] || { qty: piece.quantity, leftQty: piece.left_qty, rightQty: piece.right_qty }
                                      const isEditing = isPiecesEditMode
                                      return (
                                        <tr key={idx} className="border-b border-border/30 hover:bg-muted/20">
                                          {/* Tiny piece preview using actual vertices or bbox fallback */}
                                          <td className="py-1 px-2">
                                            <div className="w-6 h-6 bg-muted/50 rounded border border-border flex items-center justify-center overflow-hidden">
                                              {piece.bbox ? (
                                                <svg
                                                  viewBox={`0 0 ${Math.max(piece.bbox.width, 1)} ${Math.max(piece.bbox.height, 1)}`}
                                                  className="w-5 h-5"
                                                  preserveAspectRatio="xMidYMid meet"
                                                >
                                                  {piece.vertices && piece.vertices.length >= 3 ? (
                                                    // Draw actual piece outline from vertices
                                                    <polygon
                                                      points={piece.vertices.map(v => `${v[0]},${v[1]}`).join(' ')}
                                                      fill="hsl(var(--primary) / 0.2)"
                                                      stroke="hsl(var(--primary))"
                                                      strokeWidth={Math.max(piece.bbox.width, piece.bbox.height) * 0.03}
                                                      strokeLinejoin="round"
                                                    />
                                                  ) : (
                                                    // Fallback to rectangle if no vertices
                                                    <rect
                                                      x="0"
                                                      y="0"
                                                      width={piece.bbox.width}
                                                      height={piece.bbox.height}
                                                      fill="hsl(var(--primary) / 0.2)"
                                                      stroke="hsl(var(--primary))"
                                                      strokeWidth={Math.max(piece.bbox.width, piece.bbox.height) * 0.05}
                                                      rx={Math.max(piece.bbox.width, piece.bbox.height) * 0.05}
                                                    />
                                                  )}
                                                </svg>
                                              ) : (
                                                <div className="w-4 h-4 bg-primary/20 rounded" />
                                              )}
                                            </div>
                                          </td>
                                          {/* Piece name */}
                                          <td className="py-1 px-2 truncate max-w-[150px]" title={piece.name}>
                                            {piece.name}
                                          </td>
                                          {/* Quantity */}
                                          <td className="text-center py-1 px-2">
                                            {isEditing ? (
                                              <input
                                                type="number"
                                                value={edit.qty}
                                                onChange={(e) => setEditablePieces({
                                                  ...editablePieces,
                                                  [activeNestingFabric]: {
                                                    ...editables,
                                                    [piece.name]: { ...edit, qty: parseInt(e.target.value) || 0 }
                                                  }
                                                })}
                                                className="w-10 px-1 py-0.5 border rounded text-center text-xs"
                                                min={0}
                                                max={20}
                                              />
                                            ) : (
                                              <span className="font-medium">{edit.qty}</span>
                                            )}
                                          </td>
                                          {/* L/R */}
                                          <td className="text-center py-1 px-2">
                                            {piece.has_left_right ? (
                                              isEditing ? (
                                                <div className="flex items-center justify-center gap-0.5">
                                                  <input
                                                    type="number"
                                                    value={edit.leftQty}
                                                    onChange={(e) => setEditablePieces({
                                                      ...editablePieces,
                                                      [activeNestingFabric]: {
                                                        ...editables,
                                                        [piece.name]: { ...edit, leftQty: parseInt(e.target.value) || 0 }
                                                      }
                                                    })}
                                                    className="w-6 px-0.5 py-0.5 border rounded text-center text-[10px]"
                                                    min={0}
                                                    max={10}
                                                  />
                                                  <span className="text-muted-foreground">/</span>
                                                  <input
                                                    type="number"
                                                    value={edit.rightQty}
                                                    onChange={(e) => setEditablePieces({
                                                      ...editablePieces,
                                                      [activeNestingFabric]: {
                                                        ...editables,
                                                        [piece.name]: { ...edit, rightQty: parseInt(e.target.value) || 0 }
                                                      }
                                                    })}
                                                    className="w-6 px-0.5 py-0.5 border rounded text-center text-[10px]"
                                                    min={0}
                                                    max={10}
                                                  />
                                                </div>
                                              ) : (
                                                <span className="text-amber-600 text-[10px]">L{edit.leftQty}/R{edit.rightQty}</span>
                                              )
                                            ) : (
                                              <span className="text-muted-foreground">-</span>
                                            )}
                                          </td>
                                          {/* Edit button */}
                                          <td className="text-center py-1 px-1">
                                            <button
                                              onClick={() => setIsPiecesEditMode(!isPiecesEditMode)}
                                              className="p-1 hover:bg-muted rounded"
                                              title={isEditing ? 'Done' : 'Edit'}
                                            >
                                              {isEditing ? (
                                                <CheckCircle2 className="h-3 w-3 text-green-600" />
                                              ) : (
                                                <Pencil className="h-3 w-3 text-muted-foreground" />
                                              )}
                                            </button>
                                          </td>
                                        </tr>
                                      )
                                    })}
                                  </tbody>
                                </table>
                              </div>
                            </div>

                            {/* Run Nesting Button */}
                            <div className="mt-3">
                              {fabric ? (
                                <Button
                                  onClick={() => {
                                    const params = new URLSearchParams({
                                      fabric: activeNestingFabric,
                                      width: String(config.widthInches),
                                      maxBundles: String(config.maxBundles),
                                      topN: String(config.topN),
                                      fullCoverage: String(config.fullCoverage),
                                    })
                                    router.push(`/orders/${orderId}/nesting?${params.toString()}`)
                                  }}
                                  className="w-full"
                                >
                                  <Play className="mr-2 h-4 w-4" />
                                  Run GPU Nesting for {activeNestingFabric}
                                </Button>
                              ) : (
                                <Link href="/settings/fabrics" className="block">
                                  <Button variant="outline" className="w-full">
                                    <Settings className="mr-2 h-4 w-4" />
                                    Create Fabric Record for {activeNestingFabric}
                                  </Button>
                                </Link>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })()}

                    {/* Materials in pattern but NOT in order */}
                    {pattern.available_materials.filter(m => !orderFabricCodes.includes(m)).length > 0 && (
                      <div className="text-xs text-muted-foreground bg-muted/30 rounded-lg p-3">
                        <strong>Skipped materials</strong> (not in this order): {' '}
                        {pattern.available_materials.filter(m => !orderFabricCodes.includes(m)).join(', ')}
                      </div>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          )
        })()}

        {/* Nesting In Progress Section - shown while nesting is running */}
        {nestingJobs.some(j => j.status === 'running') && (() => {
          const runningJobs = nestingJobs.filter(j => j.status === 'running')
          return (
            <Card className="border-blue-200 bg-blue-50/30">
              <CardHeader>
                <div className="flex items-center gap-3">
                  <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center">
                    <Loader2 className="h-5 w-5 text-white animate-spin" />
                  </div>
                  <div className="flex-1">
                    <CardTitle>Nesting in Progress</CardTitle>
                    <CardDescription>
                      {runningJobs.length} job(s) running — results update automatically
                    </CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  {runningJobs.map((job) => {
                    const progress = job.progress || 0
                    const resultCount = job.results?.length || 0
                    return (
                      <div key={job.id} className="p-4 bg-white rounded-lg border border-blue-100">
                        <div className="flex items-center justify-between mb-2">
                          <div className="font-medium text-sm">
                            {job.fabric_width_inches}" wide × {job.max_bundle_count} max bundles
                          </div>
                          <span className="text-xs text-blue-600 font-medium">
                            {progress.toFixed(0)}%
                          </span>
                        </div>
                        {/* Progress bar */}
                        <div className="w-full bg-blue-100 rounded-full h-2 mb-2">
                          <div
                            className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                            style={{ width: `${Math.max(progress, 2)}%` }}
                          />
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {job.progress_message || 'Starting...'}
                          {resultCount > 0 && ` — ${resultCount} markers found`}
                        </div>
                        {/* Show incremental results */}
                        {job.results && job.results.length > 0 && (
                          <div className="mt-3 grid gap-2 sm:grid-cols-2 md:grid-cols-3">
                            {job.results.slice(0, 6).map((result, idx) => (
                              <div
                                key={result.id}
                                className={`p-2 rounded border text-xs ${idx === 0 ? 'bg-blue-50 border-blue-300' : 'bg-muted/30'}`}
                              >
                                <div className="flex items-center justify-between">
                                  <span className="font-mono">{result.ratio_str}</span>
                                  <span className={`font-medium ${(result.efficiency * 100) >= 80 ? 'text-green-600' : (result.efficiency * 100) >= 75 ? 'text-amber-600' : 'text-red-600'}`}>
                                    {(result.efficiency * 100).toFixed(1)}%
                                  </span>
                                </div>
                                <div className="text-muted-foreground mt-1">
                                  {result.bundle_count} bundles • {result.length_yards.toFixed(2)} yd
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                        {job.results && job.results.length > 6 && (
                          <p className="text-xs text-muted-foreground mt-2">
                            +{job.results.length - 6} more markers
                          </p>
                        )}
                        {/* Cancel button */}
                        <div className="mt-3 pt-3 border-t border-blue-100">
                          <Button
                            variant="outline"
                            size="sm"
                            className="text-red-600 border-red-200 hover:bg-red-50"
                            onClick={async () => {
                              try {
                                await api.cancelNestingJob(job.id)
                                toast({ title: 'Cancellation requested', description: 'Job will stop at next checkpoint' })
                                loadData()
                              } catch (e) {
                                toast({ title: 'Failed to cancel', variant: 'destructive' })
                              }
                            }}
                          >
                            Stop Nesting
                          </Button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </CardContent>
            </Card>
          )
        })()}

        {/* Nesting Results Section - shown after nesting is complete */}
        {nestingJobs.some(j => j.status === 'completed') && (
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
                  <Button variant="outline" size="sm" onClick={handleStartNesting}>
                    Re-run
                  </Button>
                  <Button size="sm" onClick={() => {
                    setShowCutplanConfig(true)
                    setTimeout(() => document.getElementById('cutplan-config-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
                  }}>
                    Generate Cutplan Options
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Cutplan Configuration Modal */}
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
                {order && order.order_lines && order.order_lines.length > 0 && (
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
                      onChange={(e) => setCutplanConfig({
                        ...cutplanConfig,
                        maxPlyHeight: parseInt(e.target.value) || 100
                      })}
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
                      onChange={(e) => setCutplanConfig({
                        ...cutplanConfig,
                        fabricCostPerYard: parseFloat(e.target.value) || 3.0
                      })}
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
                              setCutplanConfig({
                                ...cutplanConfig,
                                strategies: cutplanConfig.strategies.filter(s => s !== strategy.id)
                              })
                            } else {
                              setCutplanConfig({
                                ...cutplanConfig,
                                strategies: [...cutplanConfig.strategies, strategy.id]
                              })
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
                {/* Progress bar */}
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
                {/* Cancel button */}
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

        {/* Cutplans */}
        {cutplans.length > 0 && (() => {
          // Get sizes from order demand (not pattern — pattern may have extra sizes)
          const orderSizesSet = new Set<string>()
          const seenColors = new Set<string>()
          order.order_lines.forEach(line => {
            if (seenColors.has(line.color_code)) return
            seenColors.add(line.color_code)
            line.size_quantities.forEach(sq => {
              if (sq.quantity > 0) orderSizesSet.add(sq.size_code)
            })
          })
          const sizes = Array.from(orderSizesSet).sort()

          return (
            <Card>
              <CardHeader>
                <CardTitle>Cutplan Options</CardTitle>
                <CardDescription>Compare and select the best cutplan for production</CardDescription>
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
                          {plan.status === 'approved' ? (
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

                      {/* Marker Table - matching sample_cutplan format */}
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
                                <th className="text-center py-2 px-3 font-medium">Plies</th>
                                <th className="text-center py-2 px-3 font-medium">Cuts</th>
                              </tr>
                            </thead>
                            <tbody>
                              {plan.markers.map((marker, idx) => {
                                const ratioValues = marker.ratio_str.split('-').map(v => parseInt(v) || 0)
                                const bundles = ratioValues.reduce((a, b) => a + b, 0)
                                return (
                                  <tr key={marker.id || idx} className="border-b border-border/50 hover:bg-muted/20">
                                    <td className="py-2 px-3 font-medium">M{idx + 1}</td>
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
                    </div>
                  )
                })}
              </CardContent>
            </Card>
          )
        })()}

        {/* Spacer for sticky action bar */}
        <div className="h-20" />
      </div>

      {/* Sticky Action Bar */}
      {(() => {
        const pattern = order.pattern_id ? patterns.find(p => p.id === order.pattern_id) : null
        const orderFabricCodes = Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
        const orderFabricsInPattern = pattern
          ? orderFabricCodes.filter(code => pattern.available_materials.includes(code))
          : []
        const allConfigured = orderFabricsInPattern.every(code => {
          const fabric = fabrics.find(f => f.code === code)
          return !!fabric
        })
        const isConfigured = pattern && orderFabricsInPattern.length > 0 && allConfigured
        const hasNestingResults = nestingJobs.some(j => j.status === 'completed')
        const isNesting = nestingJobs.some(j => j.status === 'running')
        const hasCutplans = cutplans.length > 0
        const hasApprovedCutplan = cutplans.some(c => c.status === 'approved')

        // Determine current step
        let currentStep = 1
        let stepLabel = 'Upload Order'
        let nextAction: { label: string; onClick: () => void; disabled?: boolean } | null = null

        if (order.order_lines.length > 0) {
          currentStep = 2
          stepLabel = 'Link Pattern'
        }
        if (order.pattern_id) {
          currentStep = 3
          stepLabel = 'Configure Nesting'
        }
        if (isConfigured) {
          currentStep = 4
          stepLabel = 'Ready to Nest'
          const materialCount = orderFabricsInPattern.length
          nextAction = {
            label: isNesting ? 'Nesting in Progress...' : `Nest ${materialCount} Material${materialCount > 1 ? 's' : ''}`,
            onClick: handleStartNesting,
            disabled: isNesting,
          }
        }
        if (hasNestingResults) {
          currentStep = 5
          stepLabel = 'Nesting Complete'
          nextAction = {
            label: 'Generate Cutplan Options',
            onClick: () => {
              setShowCutplanConfig(true)
              setTimeout(() => document.getElementById('cutplan-config-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
            },
          }
        }
        if (hasCutplans) {
          currentStep = 5
          stepLabel = 'Review Cutplans'
          nextAction = null
        }
        if (hasApprovedCutplan) {
          currentStep = 6
          stepLabel = 'Complete'
          nextAction = null
        }

        return (
          <div className="fixed bottom-0 left-0 right-0 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 border-t z-50">
            <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3">
              <div className="flex items-center justify-between">
                {/* Progress indicator */}
                <div className="flex items-center gap-3">
                  <div className="flex items-center gap-1">
                    {[1, 2, 3, 4, 5, 6].map((step) => (
                      <div
                        key={step}
                        className={`w-2 h-2 rounded-full transition-all ${
                          step < currentStep
                            ? 'bg-green-500'
                            : step === currentStep
                            ? 'bg-primary w-4'
                            : 'bg-muted-foreground/30'
                        }`}
                      />
                    ))}
                  </div>
                  <span className="text-sm font-medium">
                    Step {currentStep}: {stepLabel}
                  </span>
                </div>

                {/* Action buttons */}
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground hidden sm:inline">
                    {order.order_number}
                  </span>
                  {nextAction && (
                    <Button
                      onClick={nextAction.onClick}
                      disabled={nextAction.disabled}
                      size="sm"
                    >
                      {nextAction.disabled && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                      {!nextAction.disabled && <Play className="mr-2 h-4 w-4" />}
                      {nextAction.label}
                    </Button>
                  )}
                  {hasNestingResults && !hasCutplans && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleStartNesting}
                      title="Re-run nesting with different parameters"
                    >
                      Re-run Nesting
                    </Button>
                  )}
                </div>
              </div>
            </div>
          </div>
        )
      })()}
    </DashboardLayout>
  )
}
