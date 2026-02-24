'use client'

import { useState } from 'react'
import { api, Pattern, TestMarkerResponse } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Loader2, FlaskConical, Info, ChevronDown, ChevronUp, Settings2 } from 'lucide-react'

interface TestMarkerPanelProps {
  pattern: Pattern
  fabricWidthInches: number
}

export function TestMarkerPanel({ pattern, fabricWidthInches }: TestMarkerPanelProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [isNesting, setIsNesting] = useState(false)
  const [fabricWidth, setFabricWidth] = useState(fabricWidthInches)
  const [sizeBundles, setSizeBundles] = useState<Record<string, number>>({})
  const [result, setResult] = useState<TestMarkerResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Material selection — default to SHELL if available, otherwise first
  const availableMaterials = pattern.available_materials || []
  const defaultMaterial = availableMaterials.includes('SHELL') ? 'SHELL' : (availableMaterials[0] || '')
  const [material, setMaterial] = useState(defaultMaterial)

  // Nesting parameters
  const [timeLimit, setTimeLimit] = useState(10)
  const [pieceBuffer, setPieceBuffer] = useState(2.0)
  const [edgeBuffer, setEdgeBuffer] = useState(5.0)
  const [orientation, setOrientation] = useState<'free' | 'nap_one_way'>('free')

  // All available sizes from pattern
  const availableSizes = pattern.available_sizes

  const toggleSize = (size: string) => {
    setSizeBundles(prev => {
      const next = { ...prev }
      if (next[size]) {
        delete next[size]
      } else {
        next[size] = 1
      }
      return next
    })
    setResult(null)
    setError(null)
  }

  const setBundleCount = (size: string, count: number) => {
    if (count < 0) count = 0
    if (count > 4) count = 4
    setSizeBundles(prev => {
      const next = { ...prev }
      if (count === 0) {
        delete next[size]
      } else {
        next[size] = count
      }
      return next
    })
    setResult(null)
    setError(null)
  }

  const totalBundles = Object.values(sizeBundles).reduce((sum, v) => sum + v, 0)
  const selectedSizeCount = Object.keys(sizeBundles).length
  const canNest = selectedSizeCount > 0 && totalBundles >= 1 && totalBundles <= 8

  const handleNest = async () => {
    setIsNesting(true)
    setError(null)
    setResult(null)
    try {
      const res = await api.testMarker({
        pattern_id: pattern.id,
        fabric_width_inches: fabricWidth,
        size_bundles: sizeBundles,
        material: material || undefined,
        time_limit: timeLimit,
        piece_buffer_mm: pieceBuffer,
        edge_buffer_mm: edgeBuffer,
        orientation,
      })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Test marker failed')
    } finally {
      setIsNesting(false)
    }
  }

  const efficiencyColor = (eff: number) => {
    if (eff >= 0.80) return 'bg-green-100 text-green-800 border-green-200'
    if (eff >= 0.70) return 'bg-amber-100 text-amber-800 border-amber-200'
    return 'bg-red-100 text-red-800 border-red-200'
  }

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="w-full flex items-center gap-2 px-4 py-3 rounded-lg border border-dashed border-blue-300 bg-blue-50/50 hover:bg-blue-100/50 transition-colors text-sm text-blue-700"
      >
        <FlaskConical className="h-4 w-4" />
        <span className="font-medium">Quick Test Marker</span>
        <span className="text-blue-500 ml-1">— verify pattern with a CPU vector nest</span>
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
                CPU vector nest (Spyrrow) — define ratio and nesting parameters
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
          {/* Fabric width */}
          <div className="flex items-center gap-3">
            <Label className="text-xs whitespace-nowrap w-24">Fabric Width</Label>
            <div className="flex items-center gap-1">
              <Input
                type="number"
                value={fabricWidth}
                onChange={e => { const v = e.target.value; setFabricWidth(v === '' ? '' as any : Number(v)); setResult(null) }}
                className="w-20 h-8 text-sm"
                min={20}
                max={120}
              />
              <span className="text-xs text-muted-foreground">inches</span>
            </div>
          </div>

          {/* Material selector */}
          {availableMaterials.length > 1 && (
            <div className="flex items-center gap-3">
              <Label className="text-xs whitespace-nowrap w-24">Material</Label>
              <div className="flex flex-wrap gap-1">
                {availableMaterials.map(mat => (
                  <button
                    key={mat}
                    onClick={() => { setMaterial(mat); setResult(null) }}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
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

          {/* Ratio / Size picker - all sizes in a grid */}
          <div>
            <Label className="text-xs mb-2 block">
              Marker Ratio — bundles per size
            </Label>
            {availableSizes.length === 0 ? (
              <div className="text-xs text-muted-foreground italic py-2">
                No sizes available in pattern
              </div>
            ) : (
              <div className="border rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-muted/40">
                      <th className="text-left text-xs font-medium text-muted-foreground px-3 py-1.5">Size</th>
                      <th className="text-center text-xs font-medium text-muted-foreground px-3 py-1.5">Bundles</th>
                    </tr>
                  </thead>
                  <tbody>
                    {availableSizes.map(size => {
                      const count = sizeBundles[size] || 0
                      const isSelected = count > 0
                      return (
                        <tr
                          key={size}
                          className={`border-t transition-colors ${isSelected ? 'bg-blue-50/60' : 'hover:bg-muted/20'}`}
                        >
                          <td className="px-3 py-1.5">
                            <button
                              onClick={() => {
                                if (isSelected) {
                                  setBundleCount(size, 0)
                                } else {
                                  setBundleCount(size, 1)
                                }
                              }}
                              className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                                isSelected
                                  ? 'bg-blue-600 text-white border-blue-600'
                                  : 'bg-white text-foreground border-border hover:border-blue-400'
                              }`}
                            >
                              {size}
                            </button>
                          </td>
                          <td className="px-3 py-1.5 text-center">
                            <div className="flex items-center justify-center gap-1">
                              <button
                                onClick={() => setBundleCount(size, count - 1)}
                                disabled={count === 0}
                                className="h-6 w-6 rounded border text-xs font-medium hover:bg-muted/50 disabled:opacity-30 disabled:cursor-not-allowed"
                              >
                                -
                              </button>
                              <span className={`w-6 text-center text-sm font-mono ${count > 0 ? 'font-bold text-blue-700' : 'text-muted-foreground'}`}>
                                {count}
                              </span>
                              <button
                                onClick={() => setBundleCount(size, count + 1)}
                                disabled={totalBundles >= 8}
                                className="h-6 w-6 rounded border text-xs font-medium hover:bg-muted/50 disabled:opacity-30 disabled:cursor-not-allowed"
                              >
                                +
                              </button>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {selectedSizeCount > 0 && (
              <p className="text-xs text-muted-foreground mt-2">
                {totalBundles} bundle{totalBundles !== 1 ? 's' : ''} across {selectedSizeCount} size{selectedSizeCount !== 1 ? 's' : ''}
                {totalBundles > 8 && <span className="text-red-600 ml-1">(max 8)</span>}
              </p>
            )}
          </div>

          {/* Advanced Parameters Toggle */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <Settings2 className="h-3.5 w-3.5" />
            <span>Nesting Parameters</span>
            {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>

          {showAdvanced && (
            <div className="space-y-3 pl-1 border-l-2 border-blue-200 ml-1 py-1">
              {/* Time Limit */}
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Time Limit</Label>
                <div className="flex items-center gap-1">
                  <Input
                    type="number"
                    value={timeLimit}
                    onChange={e => { const v = e.target.value; setTimeLimit(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm"
                    min={1}
                    max={60}
                    step={1}
                  />
                  <span className="text-xs text-muted-foreground">sec</span>
                </div>
              </div>

              {/* Piece Buffer */}
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Piece Buffer</Label>
                <div className="flex items-center gap-1">
                  <Input
                    type="number"
                    value={pieceBuffer}
                    onChange={e => { const v = e.target.value; setPieceBuffer(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm"
                    min={0}
                    max={10}
                    step={0.5}
                  />
                  <span className="text-xs text-muted-foreground">mm</span>
                </div>
              </div>

              {/* Edge Buffer */}
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Edge Buffer</Label>
                <div className="flex items-center gap-1">
                  <Input
                    type="number"
                    value={edgeBuffer}
                    onChange={e => { const v = e.target.value; setEdgeBuffer(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm"
                    min={0}
                    max={20}
                    step={0.5}
                  />
                  <span className="text-xs text-muted-foreground">mm</span>
                </div>
              </div>

              {/* Orientation */}
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Orientation</Label>
                <div className="flex gap-1">
                  <button
                    onClick={() => setOrientation('free')}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                      orientation === 'free'
                        ? 'bg-blue-600 text-white border-blue-600'
                        : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}
                  >
                    Free (0/180)
                  </button>
                  <button
                    onClick={() => setOrientation('nap_one_way')}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                      orientation === 'nap_one_way'
                        ? 'bg-blue-600 text-white border-blue-600'
                        : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}
                  >
                    Nap One-Way (0 only)
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Nest button */}
          <Button
            onClick={handleNest}
            disabled={!canNest || isNesting}
            size="sm"
            className="w-full"
          >
            {isNesting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Nesting...
              </>
            ) : (
              <>
                <FlaskConical className="mr-2 h-4 w-4" />
                Nest Test Marker
              </>
            )}
          </Button>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
              {error}
            </div>
          )}

          {/* Result */}
          {result && (
            <div className="space-y-3 pt-2 border-t border-blue-200">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className={`px-3 py-2 rounded-lg border text-center ${efficiencyColor(result.efficiency)}`}>
                  <div className="text-lg font-bold">{(result.efficiency * 100).toFixed(1)}%</div>
                  <div className="text-xs">Utilization</div>
                </div>
                <div className="px-3 py-2 rounded-lg border bg-muted/30 text-center">
                  <div className="text-lg font-bold">{result.length_yards.toFixed(2)}</div>
                  <div className="text-xs">Yards</div>
                </div>
                <div className="px-3 py-2 rounded-lg border bg-muted/30 text-center">
                  <div className="text-lg font-bold">{result.piece_count}</div>
                  <div className="text-xs">Pieces</div>
                </div>
                <div className="px-3 py-2 rounded-lg border bg-muted/30 text-center">
                  <div className="text-lg font-bold">{(result.computation_time_ms / 1000).toFixed(1)}</div>
                  <div className="text-xs">Seconds</div>
                </div>
              </div>

              <div className="text-xs text-muted-foreground">
                Ratio: <span className="font-mono">{result.ratio_str}</span>
              </div>

              {/* SVG Preview */}
              {result.svg_preview && (
                <div
                  className="rounded-lg border bg-white p-2 overflow-x-auto"
                  dangerouslySetInnerHTML={{ __html: result.svg_preview }}
                />
              )}

              <div className="flex items-start gap-1.5 text-xs text-muted-foreground bg-muted/20 rounded-md p-2">
                <Info className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                <span>CPU vector nest — actual piece shapes and placements. For production cutplans, use the full nesting pipeline.</span>
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
