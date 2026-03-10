'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { api, PatternPiece } from '@/lib/api'
import { TestMarkerPanel } from '@/components/test-marker-panel'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { useOrderContext } from '../order-context'
import {
  Play,
  CheckCircle2,
  Package,
  Settings,
  AlertCircle,
  Pencil,
  FlipHorizontal,
} from 'lucide-react'

export default function ConfigurePage() {
  const router = useRouter()
  const { toast } = useToast()
  const {
    order,
    orderId,
    patterns,
    fabrics,
    patternPieces,
    currentPattern,
    orderFabricCodes,
    orderSizes,
    loadData,
  } = useOrderContext()

  // Local state
  // Sizes: start with intersection of order sizes and pattern sizes, user can toggle
  const [selectedSizes, setSelectedSizes] = useState<string[]>([])
  const [materialMappings, setMaterialMappings] = useState<Record<string, string>>({})
  const [perFabricConfig, setPerFabricConfig] = useState<Record<string, {
    widthInches: number
    maxBundles: number
    maxMarkerLengthYards: number
    topN: number
    fullCoverage: boolean
    gpuScale: number
    strategy: string
    additionalWidths: string
  }>>({})
  const [activeNestingFabric, setActiveNestingFabric] = useState<string>('')
  const [editablePieces, setEditablePieces] = useState<Record<string, Record<string, { qty: number; leftQty: number; rightQty: number }>>>({})
  const [isPiecesEditMode, setIsPiecesEditMode] = useState(false)
  const [isSavingMappings, setIsSavingMappings] = useState(false)
  const [nestingConfig, setNestingConfig] = useState({
    fabricWidthInches: 60,
    maxBundleCount: 6,
    topNResults: 10,
    fullCoverage: false,
  })

  // Initialize local state from context data
  useEffect(() => {
    if (!order || !currentPattern) return

    // Initialize selected sizes from intersection of order and pattern sizes
    setSelectedSizes(prev => {
      if (prev.length > 0) return prev  // Don't reset if already set
      return currentPattern.available_sizes.filter(s => orderSizes.includes(s))
    })

    // Initialize material mappings from pattern's fabric_mappings
    const mappings: Record<string, string> = {}
    currentPattern.fabric_mappings?.forEach(m => {
      if (m.fabric_id) mappings[m.material_name] = m.fabric_id
    })
    setMaterialMappings(mappings)

    // Initialize editable pieces
    const editables: Record<string, Record<string, { qty: number; leftQty: number; rightQty: number }>> = {}
    Object.entries(patternPieces).forEach(([material, pieces]) => {
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

    // Initialize per-fabric nesting config
    const fabricConfigs: Record<string, { widthInches: number; maxBundles: number; maxMarkerLengthYards: number; topN: number; fullCoverage: boolean; gpuScale: number; strategy: string; additionalWidths: string }> = {}
    orderFabricCodes.forEach(code => {
      const fabric = fabrics.find(f => f.code === code)
      fabricConfigs[code] = {
        widthInches: fabric?.width_inches || 60,
        maxBundles: 6,
        maxMarkerLengthYards: 15,
        topN: 10,
        fullCoverage: false,
        gpuScale: 0.15,
        strategy: 'auto',
        additionalWidths: '',
      }
    })
    setPerFabricConfig(fabricConfigs)

    // Set active nesting fabric
    const orderFabricsInPattern = orderFabricCodes.filter(code =>
      currentPattern.available_materials.includes(code)
    )
    if (orderFabricsInPattern.length > 0 && !activeNestingFabric) {
      setActiveNestingFabric(orderFabricsInPattern[0])
    }

    // Initialize nesting config from first fabric
    if (orderFabricCodes.length > 0) {
      const matchedFabric = fabrics.find(f => f.code === orderFabricCodes[0])
      if (matchedFabric) {
        setNestingConfig(prev => ({
          ...prev,
          fabricWidthInches: matchedFabric.width_inches || 60,
        }))
      }
    }
  }, [order?.id, currentPattern?.id, patternPieces, fabrics.length])

  if (!order || !currentPattern) {
    return (
      <Card>
        <CardContent className="py-12 text-center">
          <p className="text-muted-foreground">
            Please select a pattern on the{' '}
            <Link href={`/orders/${orderId}`} className="text-primary underline">order page</Link>
            {' '}first.
          </p>
        </CardContent>
      </Card>
    )
  }

  const pattern = currentPattern

  // Auto-match helpers
  const getAutoMatchedMappings = () => {
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

  const autoMatched = getAutoMatchedMappings()
  const effectiveMappings = { ...autoMatched, ...materialMappings }

  const orderFabricsInPattern = orderFabricCodes.filter(code =>
    pattern.available_materials.includes(code)
  )
  const allOrderFabricsMapped = orderFabricsInPattern.every(code => {
    const fabric = fabrics.find(f => f.code === code)
    return fabric && (effectiveMappings[code] || autoMatched[code])
  })
  const isReadyForNesting = orderFabricsInPattern.length > 0 && allOrderFabricsMapped

  const handleSaveMappings = async () => {
    if (!order.pattern_id) return

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
    if (!order.pattern_id) {
      toast({ title: 'No pattern selected', variant: 'destructive' })
      return
    }

    if (!isReadyForNesting) {
      toast({ title: 'Configuration required', description: 'Please configure fabric records for all order fabrics', variant: 'destructive' })
      return
    }

    try {
      const fabricConfig = activeNestingFabric ? perFabricConfig[activeNestingFabric] : undefined
      const primaryWidth = fabricConfig?.widthInches || nestingConfig.fabricWidthInches

      // Parse additional widths into fabric_widths array
      let fabricWidths: number[] | undefined = undefined
      if (fabricConfig?.additionalWidths) {
        const extras = fabricConfig.additionalWidths
          .split(',')
          .map(s => parseFloat(s.trim()))
          .filter(v => !isNaN(v) && v > 0)
        if (extras.length > 0) {
          fabricWidths = [primaryWidth, ...extras]
        }
      }

      await api.createNestingJob({
        order_id: orderId,
        pattern_id: order.pattern_id,
        fabric_width_inches: primaryWidth,
        fabric_widths: fabricWidths,
        max_bundle_count: fabricConfig?.maxBundles || nestingConfig.maxBundleCount,
        top_n_results: fabricConfig?.topN || nestingConfig.topNResults,
        full_coverage: fabricConfig?.fullCoverage || nestingConfig.fullCoverage,
        gpu_scale: fabricConfig?.gpuScale || 0.15,
        selected_sizes: selectedSizes.length > 0 ? selectedSizes : undefined,
        strategy: fabricConfig?.strategy || 'auto',
      })
      toast({ title: 'Nesting job started', description: 'The GPU nesting job has been queued' })
      loadData()

      // Navigate to nesting page
      const params = new URLSearchParams({
        fabric: activeNestingFabric,
        width: String(fabricConfig?.widthInches || nestingConfig.fabricWidthInches),
        maxBundles: String(fabricConfig?.maxBundles || nestingConfig.maxBundleCount),
        topN: String(fabricConfig?.topN || nestingConfig.topNResults),
        fullCoverage: String(fabricConfig?.fullCoverage || nestingConfig.fullCoverage),
        gpuScale: String(fabricConfig?.gpuScale || 0.15),
        strategy: fabricConfig?.strategy || 'auto',
      })
      router.push(`/orders/${orderId}/nesting?${params.toString()}`)
    } catch (error) {
      toast({
        title: 'Failed to start nesting',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    }
  }

  return (
    <>
      {/* Configure Section */}
      <Card className="border-accent/30">
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-accent to-accent/70 flex items-center justify-center">
                <Settings className="h-5 w-5 text-accent-foreground" />
              </div>
              <div>
                <CardTitle>Configure GPU Nesting</CardTitle>
                <CardDescription>
                  Match order fabrics to pattern materials and set GPU nesting parameters
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
                {/* Left: Order Fabrics */}
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
                            Create a fabric record for &quot;{fabricCode}&quot; in{' '}
                            <Link href="/settings/fabrics" className="underline">Settings → Fabrics</Link>
                          </p>
                        )}
                      </div>
                    )
                  })}
                </div>

                {/* Right: Pattern-only materials */}
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

            {/* Size Summary */}
            {(() => {
              const missingSizes = orderSizes.filter(s => !pattern.available_sizes.includes(s))
              return missingSizes.length > 0 ? (
                <div className="pt-4 border-t">
                  <div className="flex flex-wrap gap-2 items-center">
                    <span className="text-xs font-medium text-red-700">Sizes missing in pattern:</span>
                    {missingSizes.map((size) => (
                      <span
                        key={size}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-100 text-red-700 border border-red-200"
                      >
                        {size}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null
            })()}

            {/* Size Selection */}
            <div className="pt-4 border-t">
              <div className="flex items-center justify-between mb-2">
                <div>
                  <h4 className="font-medium text-sm">Sizes for Nesting</h4>
                  <p className="text-xs text-muted-foreground">
                    {selectedSizes.length} of {currentPattern.available_sizes.length} sizes selected
                    {selectedSizes.length < orderSizes.filter(s => currentPattern.available_sizes.includes(s)).length && (
                      <span className="text-amber-600 ml-1">(some order sizes excluded)</span>
                    )}
                  </p>
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={() => setSelectedSizes(currentPattern.available_sizes.filter(s => orderSizes.includes(s)))}
                    className="text-[10px] px-2 py-0.5 rounded border hover:bg-muted"
                  >
                    Order sizes
                  </button>
                  <button
                    onClick={() => setSelectedSizes([...currentPattern.available_sizes])}
                    className="text-[10px] px-2 py-0.5 rounded border hover:bg-muted"
                  >
                    All
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {currentPattern.available_sizes.map((size) => {
                  const inOrder = orderSizes.includes(size)
                  const isSelected = selectedSizes.includes(size)
                  return (
                    <button
                      key={size}
                      onClick={() => {
                        setSelectedSizes(prev =>
                          isSelected
                            ? prev.filter(s => s !== size)
                            : [...prev, size]
                        )
                      }}
                      className={`px-3 py-1.5 rounded-md text-xs font-medium border transition-colors ${
                        isSelected
                          ? inOrder
                            ? 'bg-primary text-primary-foreground border-primary'
                            : 'bg-blue-100 text-blue-700 border-blue-300'
                          : 'bg-muted/30 text-muted-foreground border-border hover:border-primary/50'
                      }`}
                      title={inOrder ? 'In order demand' : 'Pattern-only size (not in order)'}
                    >
                      {size}
                      {inOrder && <span className="ml-1 opacity-60">*</span>}
                    </button>
                  )
                })}
              </div>
              <p className="text-[10px] text-muted-foreground mt-1">* = size present in order demand</p>
            </div>

            {/* Nesting Preview - Tabbed by Fabric */}
            <div className="space-y-4 pt-4 border-t">
              <div>
                <h4 className="font-medium text-sm">GPU Nesting Preview</h4>
                <p className="text-xs text-muted-foreground">
                  Select a fabric to configure and run GPU nesting
                </p>
              </div>

              {/* Fabric Selection Tabs */}
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
                  gpuScale: 0.15,
                  strategy: 'auto',
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

                    {/* Nesting Parameters */}
                    <div className="px-4 py-3 bg-muted/20 border-b">
                      <div className="grid grid-cols-4 gap-3 text-xs">
                        <div>
                          <label className="text-muted-foreground block mb-1">Fabric Width</label>
                          <div className="flex items-center gap-1">
                            <input
                              type="number"
                              value={config.widthInches}
                              onChange={(e) => {
                                const v = e.target.value
                                setPerFabricConfig({
                                  ...perFabricConfig,
                                  [activeNestingFabric]: { ...config, widthInches: v === '' ? '' as any : parseFloat(v) }
                                })
                              }}
                              className="w-16 px-2 py-1 border rounded text-sm"
                              min={30}
                              max={120}
                              step={0.5}
                            />
                            <span className="text-muted-foreground">&quot;</span>
                          </div>
                          <div className="mt-1.5">
                            <label className="text-muted-foreground/70 block mb-0.5" style={{ fontSize: '10px' }}>Additional Widths</label>
                            <input
                              type="text"
                              value={config.additionalWidths}
                              onChange={(e) => {
                                setPerFabricConfig({
                                  ...perFabricConfig,
                                  [activeNestingFabric]: { ...config, additionalWidths: e.target.value }
                                })
                              }}
                              placeholder="e.g. 54, 62"
                              className="w-24 px-2 py-0.5 border rounded text-xs text-muted-foreground"
                            />
                          </div>
                        </div>
                        <div>
                          <label className="text-muted-foreground block mb-1">Max Bundles</label>
                          <input
                            type="number"
                            value={config.maxBundles}
                            onChange={(e) => {
                              const v = e.target.value
                              setPerFabricConfig({
                                ...perFabricConfig,
                                [activeNestingFabric]: { ...config, maxBundles: v === '' ? '' as any : parseInt(v) }
                              })
                            }}
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
                              onChange={(e) => {
                                const v = e.target.value
                                setPerFabricConfig({
                                  ...perFabricConfig,
                                  [activeNestingFabric]: { ...config, maxMarkerLengthYards: v === '' ? '' as any : parseFloat(v) }
                                })
                              }}
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
                      {/* Nesting Strategy */}
                      <div className="mt-3 flex items-center gap-3">
                        <label className="text-xs text-muted-foreground whitespace-nowrap">Strategy</label>
                        <div className="flex gap-1">
                          {[
                            { value: 'auto', label: 'Auto', desc: 'Brute force for small spaces, LHS+predict for large (default)' },
                            { value: 'brute_force', label: 'Brute Force', desc: 'GPU-evaluate all ratios (thorough but slow for 1000+ ratios)' },
                            { value: 'lhs_predict', label: 'LHS + Predict', desc: 'Sample 12% via LHS, predict rest with Ridge regression (fast)' },
                          ].map(opt => (
                            <button
                              key={opt.value}
                              onClick={() => setPerFabricConfig({
                                ...perFabricConfig,
                                [activeNestingFabric]: { ...config, strategy: opt.value }
                              })}
                              className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                                (config.strategy || 'auto') === opt.value
                                  ? 'bg-primary text-primary-foreground border-primary'
                                  : 'bg-background text-foreground border-border hover:border-primary/50'
                              }`}
                              title={opt.desc}
                            >
                              {opt.label}
                            </button>
                          ))}
                        </div>
                      </div>
                      {/* GPU Resolution */}
                      <div className="mt-3 flex items-center gap-3">
                        <label className="text-xs text-muted-foreground whitespace-nowrap">GPU Resolution</label>
                        <div className="flex gap-1">
                          {[
                            { value: 0.15, label: 'Standard', desc: 'Fast (default)' },
                            { value: 0.3, label: 'High', desc: 'Better quality' },
                            { value: 0.5, label: 'Fine', desc: 'High fidelity' },
                            { value: 1.0, label: 'Demo', desc: 'Maximum quality (1 px/mm)' },
                          ].map(opt => (
                            <button
                              key={opt.value}
                              onClick={() => setPerFabricConfig({
                                ...perFabricConfig,
                                [activeNestingFabric]: { ...config, gpuScale: opt.value }
                              })}
                              className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                                (config.gpuScale || 0.15) === opt.value
                                  ? 'bg-primary text-primary-foreground border-primary'
                                  : 'bg-background text-foreground border-border hover:border-primary/50'
                              }`}
                              title={opt.desc}
                            >
                              {opt.label}
                            </button>
                          ))}
                        </div>
                        <span className="text-xs text-muted-foreground">({(config.gpuScale || 0.15).toFixed(2)} px/mm)</span>
                      </div>
                    </div>

                    {/* Nesting Sizes Indicator */}
                    <div className="px-4 py-2 bg-blue-50/50 border-b">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-medium text-muted-foreground">Nesting sizes:</span>
                        {currentPattern.available_sizes.map((size) => {
                          const isSelected = selectedSizes.includes(size)
                          return (
                            <span
                              key={size}
                              className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                                isSelected
                                  ? 'bg-primary/10 text-primary border border-primary/30'
                                  : 'bg-muted/50 text-muted-foreground/40 line-through border border-transparent'
                              }`}
                            >
                              {size}
                            </span>
                          )
                        })}
                        {selectedSizes.length < currentPattern.available_sizes.length && (
                          <span className="text-[10px] text-muted-foreground">
                            ({currentPattern.available_sizes.length - selectedSizes.length} excluded)
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Pieces Section */}
                    <div className="px-4 py-3">
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-4 text-xs">
                          <span><strong className="text-primary">{pieces.length}</strong> pieces</span>
                          <span><strong>{totalPiecesPerBundle}</strong>/bundle</span>
                          <span className="text-amber-600"><strong>{lrPieces.length}</strong> mirrored</span>
                          <span className="text-blue-600"><strong>{selectedSizes.length}</strong> sizes selected</span>
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
                                    <td className="py-1 px-2">
                                      <div className="w-6 h-6 bg-muted/50 rounded border border-border flex items-center justify-center overflow-hidden">
                                        {piece.bbox ? (
                                          <svg
                                            viewBox={`0 0 ${Math.max(piece.bbox.width, 1)} ${Math.max(piece.bbox.height, 1)}`}
                                            className="w-5 h-5"
                                            preserveAspectRatio="xMidYMid meet"
                                          >
                                            {piece.vertices && piece.vertices.length >= 3 ? (
                                              <polygon
                                                points={piece.vertices.map(v => `${v[0]},${v[1]}`).join(' ')}
                                                fill="hsl(var(--primary) / 0.2)"
                                                stroke="hsl(var(--primary))"
                                                strokeWidth={Math.max(piece.bbox.width, piece.bbox.height) * 0.03}
                                                strokeLinejoin="round"
                                              />
                                            ) : (
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
                                    <td className="py-1 px-2 truncate max-w-[150px]" title={piece.name}>
                                      {piece.name}
                                    </td>
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
                            onClick={handleStartNesting}
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

      {/* Quick Test Marker */}
      {order.pattern_id && (() => {
        if (!pattern.is_parsed) return null
        const firstFabricCode = orderFabricCodes[0]
        const fabric = fabrics.find(f => f.code === firstFabricCode)
        const width = fabric?.width_inches || nestingConfig.fabricWidthInches || 60
        return <TestMarkerPanel pattern={pattern} fabricWidthInches={width} orderId={orderId} />
      })()}
    </>
  )
}
