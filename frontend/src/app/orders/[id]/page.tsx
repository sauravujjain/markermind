'use client'

import { useState, useEffect, useRef } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { api, PatternPiece } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { useOrderContext } from './order-context'
import {
  FileText,
  Package,
  ChevronDown,
  Upload,
  Plus,
  Loader2,
} from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export default function OrderDetailPage() {
  const router = useRouter()
  const { toast } = useToast()
  const {
    order,
    orderId,
    patterns,
    fabrics,
    patternPieces,
    currentPattern,
    hasNestingResults,
    orderSizes,
    loadData,
  } = useOrderContext()

  // Local state
  const [activeFabricTab, setActiveFabricTab] = useState<string>('')
  const [showPatternUpload, setShowPatternUpload] = useState(false)
  const [patternUploadName, setPatternUploadName] = useState('')
  const [uploadFileType, setUploadFileType] = useState<'aama' | 'dxf_only' | 'vt_dxf'>('aama')
  const [selectedDxfFile, setSelectedDxfFile] = useState<File | null>(null)
  const [selectedRulFile, setSelectedRulFile] = useState<File | null>(null)
  const [isUploadingPattern, setIsUploadingPattern] = useState(false)
  const [sizeNames, setSizeNames] = useState('')
  const [showPieces, setShowPieces] = useState(false)
  const dxfInputRef = useRef<HTMLInputElement>(null)
  const rulInputRef = useRef<HTMLInputElement>(null)

  const orderFabricCodes = order
    ? Array.from(new Set(order.order_lines.map(l => l.fabric_code)))
    : []

  // Initialize active fabric tab from order
  useEffect(() => {
    if (!activeFabricTab && orderFabricCodes.length > 0) {
      setActiveFabricTab(orderFabricCodes[0])
    }
  }, [orderFabricCodes.length])

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
        uploadFileType,
        selectedDxfFile,
        uploadFileType === 'aama' ? (selectedRulFile || undefined) : undefined,
        uploadFileType === 'dxf_only' ? (sizeNames.trim() || allSizes.join(', ')) : undefined
      )

      toast({ title: 'Pattern uploaded successfully' })

      // Auto-select the new pattern
      await api.updateOrder(orderId, { pattern_id: newPattern.id })

      // Reset upload form
      setShowPatternUpload(false)
      setPatternUploadName('')
      setUploadFileType('aama')
      setSelectedDxfFile(null)
      setSelectedRulFile(null)
      setSizeNames('')

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

  if (!order) return null

  // Sizes in Excel column order (from context, backed by sort_order in DB)
  const allSizes = orderSizes

  // Get colors for first fabric (same for all fabrics)
  const firstFabricLines = order.order_lines.filter(l => l.fabric_code === orderFabricCodes[0])

  return (
    <>
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
                    const perFabricLines = order.order_lines.filter(l => l.fabric_code === order.order_lines[0]?.fabric_code)
                    const perFabricUnits = perFabricLines.reduce((acc, line) =>
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
                <div className="flex gap-1 p-1 bg-muted/50 rounded-lg">
                  {orderFabricCodes.map((fabricCode) => (
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
                  ))}
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

                  <div>
                    <label className="text-xs font-medium text-muted-foreground block mb-1">Pattern Type</label>
                    <div className="flex gap-1 p-0.5 bg-muted/50 rounded-md mb-1">
                      <button
                        type="button"
                        onClick={() => { setUploadFileType('aama'); setSelectedRulFile(null) }}
                        className={`flex-1 px-2 py-1.5 text-xs font-medium rounded transition-all ${
                          uploadFileType === 'aama'
                            ? 'bg-background shadow-sm text-foreground'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        AAMA (DXF + RUL)
                      </button>
                      <button
                        type="button"
                        onClick={() => { setUploadFileType('dxf_only'); setSelectedRulFile(null) }}
                        className={`flex-1 px-2 py-1.5 text-xs font-medium rounded transition-all ${
                          uploadFileType === 'dxf_only'
                            ? 'bg-background shadow-sm text-foreground'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        DXF Only
                      </button>
                      <button
                        type="button"
                        onClick={() => { setUploadFileType('vt_dxf'); setSelectedRulFile(null); setSizeNames('') }}
                        className={`flex-1 px-2 py-1.5 text-xs font-medium rounded transition-all ${
                          uploadFileType === 'vt_dxf'
                            ? 'bg-background shadow-sm text-foreground'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        VT DXF
                      </button>
                    </div>
                    {uploadFileType === 'dxf_only' && (
                      <p className="text-[11px] text-muted-foreground mb-1">Pre-sized pieces — no grading file needed</p>
                    )}
                    {uploadFileType === 'vt_dxf' && (
                      <p className="text-[11px] text-muted-foreground mb-1">Optitex Graded Nest — sizes auto-detected</p>
                    )}
                  </div>

                  {uploadFileType === 'dxf_only' && (
                    <div>
                      <label className="text-xs font-medium text-muted-foreground block mb-1">Size Names (from order, editable)</label>
                      <input
                        type="text"
                        value={sizeNames || allSizes.join(', ')}
                        onChange={(e) => setSizeNames(e.target.value)}
                        className="w-full px-3 py-2 border rounded-md text-sm"
                      />
                      <p className="text-[11px] text-muted-foreground mt-1">Auto-filled from order sizes — edit if pattern sizes differ</p>
                    </div>
                  )}

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

                  {uploadFileType === 'aama' && (
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
                  )}

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
                        setUploadFileType('aama')
                        setSelectedDxfFile(null)
                        setSelectedRulFile(null)
                        setSizeNames('')
                      }}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {/* Pattern Selector - only show when no pattern is assigned */}
              {!order.pattern_id && (
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
              )}

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
                      <div className="flex items-center gap-1">
                        {!hasNestingResults ? (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={async () => {
                              try {
                                await api.updateOrder(orderId, { pattern_id: null as any })
                                toast({ title: 'Pattern removed' })
                                setShowPieces(false)
                                loadData()
                              } catch (error) {
                                toast({
                                  title: 'Failed to change pattern',
                                  description: error instanceof Error ? error.message : 'Please try again',
                                  variant: 'destructive',
                                })
                              }
                            }}
                            className="text-xs"
                          >
                            Change Pattern
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">Pattern locked after nesting</span>
                        )}
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
                            {pieces.map((piece: PatternPiece, idx: number) => (
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
                                      [activeNestingFabric]: { ...config, widthInches: e.target.value === '' ? '' as any : parseFloat(e.target.value) }
                                    })}
                                    onBlur={(e) => {
                                      const v = parseFloat(e.target.value)
                                      if (!v || isNaN(v)) {
                                        setPerFabricConfig({
                                          ...perFabricConfig,
                                          [activeNestingFabric]: { ...config, widthInches: 60 }
                                        })
                                      }
                                    }}
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
                                    [activeNestingFabric]: { ...config, maxBundles: e.target.value === '' ? '' as any : parseInt(e.target.value) }
                                  })}
                                  onBlur={(e) => {
                                    const v = parseInt(e.target.value)
                                    if (!v || isNaN(v)) {
                                      setPerFabricConfig({
                                        ...perFabricConfig,
                                        [activeNestingFabric]: { ...config, maxBundles: 6 }
                                      })
                                    }
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
                                    onChange={(e) => setPerFabricConfig({
                                      ...perFabricConfig,
                                      [activeNestingFabric]: { ...config, maxMarkerLengthYards: e.target.value === '' ? '' as any : parseFloat(e.target.value) }
                                    })}
                                    onBlur={(e) => {
                                      const v = parseFloat(e.target.value)
                                      if (!v || isNaN(v)) {
                                        setPerFabricConfig({
                                          ...perFabricConfig,
                                          [activeNestingFabric]: { ...config, maxMarkerLengthYards: 15 }
                                        })
                                      }
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
                          {resultCount > 0 && ` -- ${resultCount} markers found`}
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
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {!order.pattern_id && patterns.filter(p => p.is_parsed).length === 0 && (
                <div className="text-center py-2">
                  <p className="text-sm text-muted-foreground mb-2">No patterns available</p>
                  <Link href="/patterns">
                    <Button variant="outline" size="sm">
                      Upload Pattern
                    </Button>
                  </Link>
                </div>
              )}

                {/* Failed state */}
                {!isRefining && refinementStatus?.status === 'failed' && (
                  <div className="p-4 bg-red-50 border border-red-200 rounded-lg space-y-2">
                    <div className="text-sm font-medium text-red-800">Final nesting failed</div>
                    <div className="text-xs text-red-600">{refinementStatus.message}</div>
                    <Button variant="outline" size="sm" onClick={() => setRefinementStatus(null)}>
                      Try Again
                    </Button>
                  </div>
                )}

                {/* Completed layouts */}
                {refinementStatus && refinementStatus.layouts.length > 0 && (
                  <div className="space-y-3">
                    {refinementStatus.layouts.map((layout, idx) => {
                      const ratioValues = layout.ratio_str.split('-').map(v => parseInt(v) || 0)
                      const bundles = ratioValues.reduce((a, b) => a + b, 0)
                      const utilPct = (layout.utilization * 100)
                      return (
                        <div key={layout.id} className="border rounded-lg overflow-hidden bg-white">
                          <div className="px-4 py-2 bg-muted/30 flex items-center justify-between border-b">
                            <div className="flex items-center gap-3">
                              <span className="font-semibold text-sm">M{idx + 1}</span>
                              <span className="font-mono text-xs text-muted-foreground">{layout.ratio_str}</span>
                              <span className="text-xs bg-primary/10 text-primary px-2 py-0.5 rounded">{bundles} bundles</span>
                            </div>
                            <div className="flex items-center gap-4 text-xs">
                              <span className={`font-medium ${utilPct >= 80 ? 'text-green-600' : utilPct >= 75 ? 'text-amber-600' : 'text-red-600'}`}>
                                {utilPct.toFixed(1)}%
                              </span>
                              <span className="text-muted-foreground">{layout.length_yards.toFixed(2)} yd</span>
                              <span className="text-muted-foreground">{layout.computation_time_s.toFixed(1)}s</span>
                            </div>
                          </div>
                          {/* SVG Preview */}
                          <div className="p-3 overflow-x-auto">
                            <div
                              className="min-w-[400px]"
                              dangerouslySetInnerHTML={{ __html: layout.svg_preview }}
                            />
                          </div>
                        </div>
                      )
                    })}

                    {/* Re-run / Download buttons */}
                    {!isRefining && refinementStatus.status === 'completed' && (
                      <div className="flex items-center gap-3 pt-2">
                        <Button onClick={() => handleDownloadMarkers(approvedPlan.id)}>
                          <Download className="mr-2 h-4 w-4" />
                          Download All Markers (DXF)
                        </Button>
                        <Button variant="outline" onClick={() => {
                          setRefinementStatus(null)
                        }}>
                          Re-run with Different Settings
                        </Button>
                      </div>
                    )}
                  </div>
                )}
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
          const hasRefinedCutplan = cutplans.some(c => c.status === 'refined')
          const isRefiningNow = cutplans.some(c => c.status === 'refining')
          if (hasRefinedCutplan) {
            stepLabel = 'Export Ready'
            nextAction = {
              label: 'Download Markers',
              onClick: () => {
                const plan = cutplans.find(c => c.status === 'refined')
                if (plan) handleDownloadMarkers(plan.id)
              },
            }
          } else if (isRefiningNow) {
            stepLabel = 'Final Nesting...'
            nextAction = null
          } else {
            stepLabel = 'Final Nesting'
            nextAction = {
              label: 'Start Final Nesting',
              onClick: () => {
                document.getElementById('final-nesting-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              },
            }
          }
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
                {nextAction && (
                  <Button onClick={nextAction.onClick} disabled={nextAction.disabled}>
                    {nextAction.label}
                  </Button>
                )}
              </div>
            </div>
          </div>
        )
      })()}
    </>
  )
}
