'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useAuthStore } from '@/lib/auth-store'
import { api, OrderImportRow } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { useDropzone } from 'react-dropzone'
import { Upload, FileSpreadsheet, ArrowLeft, Download, Info, CheckCircle } from 'lucide-react'
import Link from 'next/link'
import * as XLSX from 'xlsx'

/**
 * Excel Format:
 * | Order No. | Style No. | Fabric | Order Color | Extra % | 46 | 48 | 50 | ... |
 * | Order1    | style1    | SO1    | 8320        | 0       | 74 | 244| 347| ... |
 */

interface OrderLine {
  fabricCode: string
  colorCode: string
  extraPercent: number
  quantities: { [size: string]: number }
}

interface ParsedOrder {
  orderNumber: string
  styleNumber: string
  lines: OrderLine[]
  sizes: string[]
  totalGarments: number
  fabricCount: number
  colorCount: number
}

interface ParsedFile {
  orders: ParsedOrder[]
  totalOrders: number
  totalLines: number
  totalGarments: number
}

export default function NewOrderPage() {
  const router = useRouter()
  const { isAuthenticated, isLoading: authLoading, checkAuth } = useAuthStore()
  const { toast } = useToast()
  const [isLoading, setIsLoading] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [parsedFile, setParsedFile] = useState<ParsedFile | null>(null)
  const [parseError, setParseError] = useState<string | null>(null)
  const [selectedOrderIdx, setSelectedOrderIdx] = useState(0)

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.push('/login')
    }
  }, [authLoading, isAuthenticated, router])

  const parseExcelFile = async (file: File): Promise<ParsedFile> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = (e) => {
        try {
          const data = new Uint8Array(e.target?.result as ArrayBuffer)
          const workbook = XLSX.read(data, { type: 'array' })

          // Get the first sheet
          const sheetName = workbook.SheetNames[0]
          const worksheet = workbook.Sheets[sheetName]
          const jsonData = XLSX.utils.sheet_to_json(worksheet, { header: 1 }) as any[][]

          if (jsonData.length < 2) {
            throw new Error('File must have header row and at least one data row')
          }

          // Parse header row - find column indices
          const headerRow = jsonData[0].map(h => String(h || '').trim())

          const orderNoIdx = headerRow.findIndex(h => h.toLowerCase().includes('order') && h.toLowerCase().includes('no'))
          const styleNoIdx = headerRow.findIndex(h => h.toLowerCase().includes('style') && h.toLowerCase().includes('no'))
          const fabricIdx = headerRow.findIndex(h => h.toLowerCase() === 'fabric')
          const colorIdx = headerRow.findIndex(h => h.toLowerCase().includes('order') && h.toLowerCase().includes('color'))
          const extraIdx = headerRow.findIndex(h => h.toLowerCase().includes('extra'))

          if (orderNoIdx === -1) throw new Error('Missing "Order No." column')
          if (fabricIdx === -1) throw new Error('Missing "Fabric" column')
          if (colorIdx === -1) throw new Error('Missing "Order Color" column')

          // Size columns start after Extra % (or after Color if no Extra)
          const sizeStartIdx = extraIdx !== -1 ? extraIdx + 1 : colorIdx + 1
          const sizes = headerRow.slice(sizeStartIdx).filter(s => s && s.trim() !== '')

          if (sizes.length === 0) {
            throw new Error('No size columns found after "Extra %" column')
          }

          // Parse data rows and group by order number
          const orderMap = new Map<string, ParsedOrder>()

          for (let i = 1; i < jsonData.length; i++) {
            const row = jsonData[i]
            if (!row || row.length === 0) continue

            const orderNumberRaw = String(row[orderNoIdx] || '').trim()
            if (!orderNumberRaw) continue

            // Use lowercase key for grouping (case-insensitive), preserve original for display
            const orderKey = orderNumberRaw.toLowerCase()
            const styleNumber = styleNoIdx !== -1 ? String(row[styleNoIdx] || '').trim() : ''
            const fabricCode = String(row[fabricIdx] || '').trim()
            const colorCode = String(row[colorIdx] || '').trim()
            const extraPercent = extraIdx !== -1 ? parseFloat(String(row[extraIdx] || '0')) || 0 : 0

            if (!fabricCode || !colorCode) continue

            // Parse size quantities
            const quantities: { [size: string]: number } = {}
            let lineTotal = 0
            for (let j = 0; j < sizes.length; j++) {
              const qty = parseInt(String(row[sizeStartIdx + j] || '0'), 10) || 0
              if (qty > 0) {
                quantities[sizes[j]] = qty
                lineTotal += qty
              }
            }

            // Skip lines with no quantities
            if (lineTotal === 0) continue

            // Get or create order (case-insensitive grouping)
            if (!orderMap.has(orderKey)) {
              orderMap.set(orderKey, {
                orderNumber: orderNumberRaw,  // Use first occurrence's casing
                styleNumber,
                lines: [],
                sizes: [],
                totalGarments: 0,
                fabricCount: 0,
                colorCount: 0
              })
            }

            const order = orderMap.get(orderKey)!
            order.lines.push({
              fabricCode,
              colorCode,
              extraPercent,
              quantities
            })
            order.totalGarments += lineTotal

            // Track sizes used in this order
            Object.keys(quantities).forEach(size => {
              if (!order.sizes.includes(size)) {
                order.sizes.push(size)
              }
            })
          }

          if (orderMap.size === 0) {
            throw new Error('No valid order data found in file')
          }

          // Calculate fabric and color counts per order
          const orders: ParsedOrder[] = []
          let totalLines = 0
          let totalGarments = 0

          orderMap.forEach(order => {
            const fabrics = new Set(order.lines.map(l => l.fabricCode))
            const colors = new Set(order.lines.map(l => l.colorCode))
            order.fabricCount = fabrics.size
            order.colorCount = colors.size
            orders.push(order)
            totalLines += order.lines.length
            totalGarments += order.totalGarments
          })

          resolve({
            orders,
            totalOrders: orders.length,
            totalLines,
            totalGarments
          })
        } catch (err) {
          reject(err)
        }
      }
      reader.onerror = () => reject(new Error('Failed to read file'))
      reader.readAsArrayBuffer(file)
    })
  }

  const onDrop = useCallback(async (acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      const uploadedFile = acceptedFiles[0]
      setFile(uploadedFile)
      setParseError(null)
      setParsedFile(null)
      setSelectedOrderIdx(0)

      try {
        const parsed = await parseExcelFile(uploadedFile)
        setParsedFile(parsed)
      } catch (err) {
        setParseError(err instanceof Error ? err.message : 'Failed to parse file')
      }
    }
  }, [])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'application/vnd.ms-excel': ['.xls'],
      'text/csv': ['.csv'],
    },
    maxFiles: 1,
  })

  // Convert parsed data to API format
  const convertToImportRows = (orders: ParsedOrder[]): OrderImportRow[] => {
    const rows: OrderImportRow[] = []
    for (const order of orders) {
      for (const line of order.lines) {
        rows.push({
          order_number: order.orderNumber,
          style_number: order.styleNumber,
          fabric_code: line.fabricCode,
          color_code: line.colorCode,
          extra_percent: line.extraPercent,
          sizes: line.quantities,
        })
      }
    }
    return rows
  }

  const handleImportAll = async () => {
    if (!parsedFile) {
      toast({
        title: 'No file selected',
        description: 'Please upload an Excel or CSV file',
        variant: 'destructive',
      })
      return
    }

    setIsLoading(true)
    try {
      const rows = convertToImportRows(parsedFile.orders)
      const results = await api.importOrdersBatch(rows)
      toast({
        title: 'Orders imported',
        description: `${results.length} order(s) imported successfully`,
      })
      router.push('/orders')
    } catch (error) {
      toast({
        title: 'Failed to import orders',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const handleImportSingle = async (orderIdx: number) => {
    if (!parsedFile) return

    setIsLoading(true)
    try {
      const rows = convertToImportRows([parsedFile.orders[orderIdx]])
      const results = await api.importOrdersBatch(rows)
      toast({
        title: 'Order imported',
        description: `Order ${results[0].order_number} imported successfully`,
      })
      router.push(`/orders/${results[0].id}`)
    } catch (error) {
      toast({
        title: 'Failed to import order',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  if (authLoading || !isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  const selectedOrder = parsedFile?.orders[selectedOrderIdx]

  return (
    <DashboardLayout>
      <div className="max-w-5xl mx-auto space-y-6">
        <div className="flex items-center space-x-4">
          <Link href="/orders">
            <Button variant="ghost" size="icon">
              <ArrowLeft className="h-4 w-4" />
            </Button>
          </Link>
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Import Order</h1>
            <p className="text-muted-foreground">
              Upload Excel file with order quantities
            </p>
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-6">
          {/* Upload Section */}
          <Card>
            <CardHeader>
              <CardTitle>Upload Order File</CardTitle>
              <CardDescription>
                Excel format: Order No. | Style No. | Fabric | Order Color | Extra % | [Sizes...]
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Download Template Button */}
              <div className="flex gap-2">
                <a href="/templates/order_template.xlsx" download>
                  <Button variant="outline" size="sm" className="gap-2">
                    <Download className="h-4 w-4" />
                    Download Template
                  </Button>
                </a>
              </div>

              {/* Drop Zone */}
              <div
                {...getRootProps()}
                className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
                  isDragActive
                    ? 'border-primary bg-primary/5'
                    : file && parsedFile
                    ? 'border-green-500 bg-green-50'
                    : parseError
                    ? 'border-red-500 bg-red-50'
                    : 'border-gray-300 hover:border-primary'
                }`}
              >
                <input {...getInputProps()} />
                {file ? (
                  <div className="space-y-2">
                    <FileSpreadsheet className={`mx-auto h-12 w-12 ${parsedFile ? 'text-green-500' : parseError ? 'text-red-500' : 'text-gray-400'}`} />
                    <p className="text-sm font-medium">{file.name}</p>
                    {parsedFile && (
                      <p className="text-xs text-green-600 flex items-center justify-center gap-1">
                        <CheckCircle className="h-3 w-3" />
                        {parsedFile.totalOrders} order(s), {parsedFile.totalLines} lines parsed
                      </p>
                    )}
                    {parseError && (
                      <p className="text-xs text-red-600">{parseError}</p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      Click or drag to replace
                    </p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <Upload className="mx-auto h-12 w-12 text-muted-foreground" />
                    <p className="text-sm font-medium">
                      {isDragActive ? 'Drop file here' : 'Drag & drop or click to upload'}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Supports .xlsx, .xls, .csv
                    </p>
                  </div>
                )}
              </div>

              {/* File Format Info */}
              <div className="bg-blue-50 rounded-lg p-4 space-y-2">
                <div className="flex items-start gap-2">
                  <Info className="h-4 w-4 text-blue-600 mt-0.5" />
                  <div className="text-sm text-blue-800">
                    <p className="font-medium">Expected columns:</p>
                    <ul className="list-disc list-inside text-xs mt-1 space-y-0.5">
                      <li><strong>Order No.</strong> - Order identifier</li>
                      <li><strong>Style No.</strong> - Style/pattern reference</li>
                      <li><strong>Fabric</strong> - Material code (SO1, FO1, Shell...)</li>
                      <li><strong>Order Color</strong> - Color code (8320, Red...)</li>
                      <li><strong>Extra %</strong> - Buffer percentage</li>
                      <li><strong>[Size columns]</strong> - Quantities per size</li>
                    </ul>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Preview Section */}
          <Card>
            <CardHeader>
              <CardTitle>Order Preview</CardTitle>
              <CardDescription>
                {parsedFile
                  ? `${parsedFile.totalOrders} order(s), ${parsedFile.totalLines} lines, ${parsedFile.totalGarments.toLocaleString()} garments`
                  : 'Upload a file to see preview'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {parsedFile ? (
                <div className="space-y-4">
                  {/* Import All Button - Primary Action */}
                  <Button
                    onClick={handleImportAll}
                    disabled={isLoading}
                    className="w-full"
                    size="lg"
                  >
                    {isLoading ? 'Importing...' : `Import All ${parsedFile.totalOrders} Order(s)`}
                  </Button>

                  {/* Orders Summary */}
                  <div className="space-y-3">
                    {parsedFile.orders.map((order, idx) => (
                      <div
                        key={order.orderNumber}
                        className={`border rounded-lg p-3 cursor-pointer transition-colors ${
                          selectedOrderIdx === idx ? 'border-primary bg-primary/5' : 'hover:border-gray-400'
                        }`}
                        onClick={() => setSelectedOrderIdx(idx)}
                      >
                        <div className="flex items-center justify-between">
                          <div>
                            <p className="font-medium">{order.orderNumber}</p>
                            <p className="text-xs text-muted-foreground">
                              {order.styleNumber || 'No style'} • {order.fabricCount} fabric(s) • {order.colorCount} color(s)
                            </p>
                          </div>
                          <div className="text-right">
                            <p className="text-lg font-semibold">{order.totalGarments.toLocaleString()}</p>
                            <p className="text-xs text-muted-foreground">{order.lines.length} lines</p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Selected Order Details (expandable) */}
                  {selectedOrder && (
                    <div className="border-t pt-4">
                      <p className="text-sm font-medium mb-2">
                        {selectedOrder.orderNumber} Details
                      </p>
                      <div className="border rounded-lg overflow-hidden">
                        <div className="overflow-x-auto max-h-48">
                          <table className="w-full text-xs">
                            <thead className="bg-gray-50 sticky top-0">
                              <tr>
                                <th className="text-left p-2 font-medium">Fabric</th>
                                <th className="text-left p-2 font-medium">Color</th>
                                <th className="text-center p-2 font-medium">Extra%</th>
                                {selectedOrder.sizes.map(size => (
                                  <th key={size} className="text-center p-2 font-medium min-w-[40px]">{size}</th>
                                ))}
                                <th className="text-center p-2 font-medium bg-gray-100">Total</th>
                              </tr>
                            </thead>
                            <tbody>
                              {selectedOrder.lines.map((line, idx) => {
                                const lineTotal = Object.values(line.quantities).reduce((a, b) => a + b, 0)
                                return (
                                  <tr key={idx} className={idx % 2 === 0 ? 'bg-white' : 'bg-gray-50'}>
                                    <td className="p-2 font-medium">{line.fabricCode}</td>
                                    <td className="p-2">{line.colorCode}</td>
                                    <td className="text-center p-2">{line.extraPercent}</td>
                                    {selectedOrder.sizes.map(size => (
                                      <td key={size} className="text-center p-2">
                                        {line.quantities[size] || ''}
                                      </td>
                                    ))}
                                    <td className="text-center p-2 font-medium bg-gray-100">{lineTotal}</td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      </div>
                      {/* Import Single Order Button */}
                      <Button
                        onClick={() => handleImportSingle(selectedOrderIdx)}
                        disabled={isLoading}
                        variant="outline"
                        className="w-full mt-3"
                        size="sm"
                      >
                        Import Only {selectedOrder.orderNumber}
                      </Button>
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center py-12 text-muted-foreground">
                  <FileSpreadsheet className="mx-auto h-12 w-12 mb-4 opacity-50" />
                  <p>No file uploaded yet</p>
                  <p className="text-xs mt-1">Upload an Excel file to preview</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Cancel Button */}
        <div className="flex justify-start">
          <Link href="/orders">
            <Button variant="outline">Cancel</Button>
          </Link>
        </div>
      </div>
    </DashboardLayout>
  )
}
