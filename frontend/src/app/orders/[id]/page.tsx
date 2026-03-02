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
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  )
}
