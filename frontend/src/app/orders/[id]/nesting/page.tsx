'use client'

import { useEffect, useState, useRef } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import Link from 'next/link'
import { useAuthStore } from '@/lib/auth-store'
import { api, Order, Pattern, NestingJob, Fabric } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { ArrowLeft, Play, Clock, CheckCircle2, Package, Layers, Zap, TrendingUp, AlertCircle, XCircle } from 'lucide-react'

interface MarkerResult {
  ratio: string
  bundles: number
  efficiency: number
  lengthYards: number
  rank: number
}

export default function NestingProgressPage() {
  const router = useRouter()
  const params = useParams()
  const searchParams = useSearchParams()
  const orderId = params.id as string
  const fabricCode = searchParams.get('fabric') || ''
  const { isAuthenticated, isLoading: authLoading, checkAuth } = useAuthStore()
  const { toast } = useToast()

  const [order, setOrder] = useState<Order | null>(null)
  const [pattern, setPattern] = useState<Pattern | null>(null)
  const [fabric, setFabric] = useState<Fabric | null>(null)
  const [nestingJob, setNestingJob] = useState<NestingJob | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isStarting, setIsStarting] = useState(false)
  const [markers, setMarkers] = useState<MarkerResult[]>([])
  const [currentMarkerPreview, setCurrentMarkerPreview] = useState<string | null>(null)
  const [currentPreviewRatio, setCurrentPreviewRatio] = useState<string>('')
  const [currentPreviewEfficiency, setCurrentPreviewEfficiency] = useState<number>(0)
  const [elapsedTime, setElapsedTime] = useState(0)
  const [startTime, setStartTime] = useState<number | null>(null)

  const resultsEndRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<NodeJS.Timeout | null>(null)
  const pollingRef = useRef<NodeJS.Timeout | null>(null)

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

  // Auto-scroll to bottom when new markers are added
  useEffect(() => {
    if (resultsEndRef.current) {
      resultsEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [markers])

  // Timer for elapsed time
  useEffect(() => {
    if (nestingJob?.status === 'running' && startTime) {
      timerRef.current = setInterval(() => {
        setElapsedTime(Math.floor((Date.now() - startTime) / 1000))
      }, 1000)
    }
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
      }
    }
  }, [nestingJob?.status, startTime])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
      }
      if (timerRef.current) {
        clearInterval(timerRef.current)
      }
    }
  }, [])

  const loadData = async () => {
    try {
      const [orderData, patternsData, fabricsData, jobsData] = await Promise.all([
        api.getOrder(orderId),
        api.getPatterns(),
        api.getFabrics(),
        api.getNestingJobs(orderId),
      ])
      setOrder(orderData)

      if (orderData.pattern_id) {
        const p = patternsData.find(pat => pat.id === orderData.pattern_id)
        setPattern(p || null)
      }

      if (fabricCode) {
        const f = fabricsData.find(fab => fab.code === fabricCode)
        setFabric(f || null)
      }

      // Check for existing job
      if (jobsData.length > 0) {
        const latestJob = jobsData[0]
        setNestingJob(latestJob)

        // If running, start polling
        if (latestJob.status === 'running' || latestJob.status === 'pending') {
          setStartTime(new Date(latestJob.created_at).getTime())
          startPolling(latestJob.id)
        }

        // Load existing results
        if (latestJob.results && latestJob.results.length > 0) {
          const sortedResults = latestJob.results
            .map(r => ({
              ratio: r.ratio_str,
              bundles: r.bundle_count,
              efficiency: r.efficiency,
              lengthYards: r.length_yards,
              rank: r.rank,
            }))
            .sort((a, b) => b.efficiency - a.efficiency)
          setMarkers(sortedResults)
        }
      }
    } catch (error) {
      toast({
        title: 'Failed to load data',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  const startPolling = (jobId: string) => {
    // Clear any existing polling
    if (pollingRef.current) {
      clearInterval(pollingRef.current)
    }

    pollingRef.current = setInterval(async () => {
      try {
        // Fetch job status and preview in parallel
        const [jobData, previewData] = await Promise.all([
          api.getNestingJob(jobId),
          api.getNestingJobPreview(jobId),
        ])

        setNestingJob(jobData)

        // Update preview if available
        if (previewData.has_preview && previewData.preview_base64) {
          setCurrentMarkerPreview(`data:image/png;base64,${previewData.preview_base64}`)
          setCurrentPreviewRatio(previewData.ratio_str || '')
          setCurrentPreviewEfficiency(previewData.efficiency || 0)
        }

        // Update results
        if (jobData.results && jobData.results.length > 0) {
          const sortedResults = jobData.results
            .map(r => ({
              ratio: r.ratio_str,
              bundles: r.bundle_count,
              efficiency: r.efficiency,
              lengthYards: r.length_yards,
              rank: r.rank,
            }))
            .sort((a, b) => b.efficiency - a.efficiency)
          setMarkers(sortedResults)
        }

        // Stop polling if job is done
        if (jobData.status === 'completed' || jobData.status === 'failed' || jobData.status === 'cancelled') {
          if (pollingRef.current) {
            clearInterval(pollingRef.current)
          }

          if (jobData.status === 'completed') {
            toast({
              title: 'Nesting Complete!',
              description: `Generated ${jobData.results?.length || 0} markers for ${fabricCode}`,
            })
          } else if (jobData.status === 'failed') {
            toast({
              title: 'Nesting Failed',
              description: jobData.error_message || 'An error occurred',
              variant: 'destructive',
            })
          }
        }
      } catch (error) {
        console.error('Polling error:', error)
      }
    }, 500) // Poll every 0.5 seconds for faster preview cycling
  }

  const startNesting = async () => {
    if (!order?.pattern_id || !fabric) return

    setIsStarting(true)
    setMarkers([])
    setCurrentMarkerPreview(null)
    setStartTime(Date.now())
    setElapsedTime(0)

    try {
      // Get config from URL params or use defaults
      const widthInches = parseFloat(searchParams.get('width') || String(fabric.width_inches || 60))
      const maxBundles = parseInt(searchParams.get('maxBundles') || '6')
      const topN = parseInt(searchParams.get('topN') || '10')

      // Create nesting job
      const job = await api.createNestingJob({
        order_id: orderId,
        pattern_id: order.pattern_id,
        fabric_width_inches: widthInches,
        max_bundle_count: maxBundles,
        top_n_results: topN,
      })

      setNestingJob(job)
      startPolling(job.id)

      toast({
        title: 'Nesting Started',
        description: `GPU nesting job submitted for ${fabricCode}`,
      })
    } catch (error) {
      toast({
        title: 'Failed to start nesting',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsStarting(false)
    }
  }

  const isNesting = nestingJob?.status === 'running' || nestingJob?.status === 'pending'
  const isComplete = nestingJob?.status === 'completed'
  const isFailed = nestingJob?.status === 'failed'

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

  const bestMarker = markers.length > 0 ? markers[0] : null
  const progress = nestingJob?.progress || 0

  return (
    <DashboardLayout>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <Link href={`/orders/${orderId}`}>
              <Button variant="ghost" size="icon">
                <ArrowLeft className="h-4 w-4" />
              </Button>
            </Link>
            <div>
              <h1 className="text-2xl font-bold tracking-tight flex items-center gap-3">
                GPU Nesting
                <span className="text-lg font-normal text-muted-foreground">•</span>
                <span className="text-primary">{fabricCode}</span>
              </h1>
              <p className="text-muted-foreground">
                {order.order_number} • {pattern?.name || 'Pattern'} • {fabric?.width_inches || 60}" wide
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link href={`/orders/${orderId}`}>
              <Button variant="outline">
                Back to Order
              </Button>
            </Link>
          </div>
        </div>

        {/* Progress Section */}
        <Card className={isNesting ? 'border-primary' : ''}>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                  isNesting ? 'bg-primary text-primary-foreground animate-pulse' :
                  isComplete ? 'bg-green-500 text-white' :
                  isFailed ? 'bg-red-500 text-white' :
                  'bg-muted'
                }`}>
                  {isFailed ? <XCircle className="h-6 w-6" /> :
                   isComplete ? <CheckCircle2 className="h-6 w-6" /> :
                   <Zap className="h-6 w-6" />}
                </div>
                <div>
                  <CardTitle>
                    {isNesting ? 'Nesting in Progress...' :
                     isComplete ? 'Nesting Complete' :
                     isFailed ? 'Nesting Failed' :
                     'Ready to Nest'}
                  </CardTitle>
                  <CardDescription>
                    {isNesting
                      ? nestingJob?.progress_message || 'Processing...'
                      : isComplete
                      ? `Generated ${markers.length} markers`
                      : isFailed
                      ? nestingJob?.error_message || 'An error occurred'
                      : `Click Start to begin GPU nesting for ${fabricCode}`
                    }
                  </CardDescription>
                </div>
              </div>
              <div className="text-right">
                <div className="text-2xl font-bold tabular-nums">
                  {formatTime(elapsedTime)}
                </div>
                <div className="text-xs text-muted-foreground">Elapsed Time</div>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {/* Large Progress Bar */}
            <div className="space-y-2 mb-6">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Progress</span>
                <span className="font-medium tabular-nums">{progress}%</span>
              </div>
              <div className="h-4 bg-muted rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all duration-300 ease-out ${
                    isFailed ? 'bg-red-500' :
                    isComplete ? 'bg-green-500' :
                    'bg-gradient-to-r from-primary to-primary/80'
                  }`}
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>

            {/* Start/Stop Button */}
            {!isNesting && !isComplete && !isFailed && (
              <Button onClick={startNesting} size="lg" className="w-full" disabled={isStarting}>
                {isStarting ? (
                  <>
                    <Clock className="mr-2 h-5 w-5 animate-spin" />
                    Starting...
                  </>
                ) : (
                  <>
                    <Play className="mr-2 h-5 w-5" />
                    Start GPU Nesting
                  </>
                )}
              </Button>
            )}

            {isNesting && (
              <Button variant="outline" size="lg" className="w-full" disabled>
                <Clock className="mr-2 h-5 w-5 animate-spin" />
                Processing...
              </Button>
            )}

            {(isComplete || isFailed) && (
              <div className="flex gap-3">
                <Button onClick={startNesting} variant="outline" className="flex-1" disabled={isStarting}>
                  <Play className="mr-2 h-4 w-4" />
                  Re-run Nesting
                </Button>
                <Link href={`/orders/${orderId}`} className="flex-1">
                  <Button className="w-full">
                    <CheckCircle2 className="mr-2 h-4 w-4" />
                    Done - Back to Order
                  </Button>
                </Link>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Results Section */}
        <div className="grid gap-6 lg:grid-cols-2">
          {/* Marker Preview */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Current Marker Preview</CardTitle>
              <CardDescription>
                {currentPreviewRatio ? `Ratio: ${currentPreviewRatio} • ${(currentPreviewEfficiency * 100).toFixed(2)}% efficiency` : 'Waiting for nesting to start...'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="bg-muted rounded-lg overflow-hidden border" style={{ height: '120px' }}>
                {currentMarkerPreview ? (
                  <img
                    src={currentMarkerPreview}
                    alt="Marker Preview"
                    className="w-full h-full object-contain"
                    style={{ imageRendering: 'pixelated' }}
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                    <Package className="h-8 w-8 mr-2 opacity-30" />
                    <span>Marker preview will appear here</span>
                  </div>
                )}
              </div>

              {/* Best Marker Stats - only efficiency and bundles, no length */}
              {bestMarker && (
                <div className="mt-4 grid grid-cols-2 gap-3">
                  <div className="bg-green-50 dark:bg-green-950/30 rounded-lg p-3 text-center border border-green-200 dark:border-green-800">
                    <div className="text-xl font-bold text-green-700 dark:text-green-400">{(bestMarker.efficiency * 100).toFixed(2)}%</div>
                    <div className="text-xs text-green-600 dark:text-green-500">Best Efficiency</div>
                  </div>
                  <div className="bg-blue-50 dark:bg-blue-950/30 rounded-lg p-3 text-center border border-blue-200 dark:border-blue-800">
                    <div className="text-xl font-bold text-blue-700 dark:text-blue-400">{bestMarker.bundles}</div>
                    <div className="text-xs text-blue-600 dark:text-blue-500">Bundles</div>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Results Table */}
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-base">Marker Results</CardTitle>
                  <CardDescription>
                    {markers.length} markers found, sorted by efficiency
                  </CardDescription>
                </div>
                {markers.length > 0 && (
                  <span className="flex items-center gap-1 text-xs text-green-700 dark:text-green-400 bg-green-100 dark:bg-green-900/30 px-2 py-1 rounded-full">
                    <TrendingUp className="h-3 w-3" />
                    {markers.length} markers
                  </span>
                )}
              </div>
            </CardHeader>
            <CardContent>
              <div className="border rounded-lg overflow-hidden">
                <div className="max-h-[400px] overflow-y-auto">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-muted/80 backdrop-blur">
                      <tr className="border-b">
                        <th className="text-left py-2 px-3 font-medium">#</th>
                        <th className="text-left py-2 px-3 font-medium">Ratio</th>
                        <th className="text-center py-2 px-3 font-medium">Bundles</th>
                        <th className="text-center py-2 px-3 font-medium">Efficiency</th>
                      </tr>
                    </thead>
                    <tbody>
                      {markers.map((marker, idx) => {
                        const effPercent = marker.efficiency * 100
                        return (
                          <tr
                            key={`${marker.ratio}-${idx}`}
                            className={`border-b border-border/50 transition-colors ${
                              idx === 0 ? 'bg-green-50 dark:bg-green-950/30' : 'hover:bg-muted/30'
                            }`}
                          >
                            <td className="py-2 px-3 text-muted-foreground">{idx + 1}</td>
                            <td className="py-2 px-3 font-mono text-xs">{marker.ratio}</td>
                            <td className="py-2 px-3 text-center">
                              <span className="bg-primary/10 text-primary px-2 py-0.5 rounded text-xs">
                                {marker.bundles}
                              </span>
                            </td>
                            <td className="py-2 px-3 text-center">
                              <span className={`font-medium ${
                                effPercent >= 80 ? 'text-green-600 dark:text-green-400' :
                                effPercent >= 75 ? 'text-amber-600 dark:text-amber-400' : 'text-red-600 dark:text-red-400'
                              }`}>
                                {effPercent.toFixed(2)}%
                              </span>
                            </td>
                          </tr>
                        )
                      })}
                      {markers.length === 0 && (
                        <tr>
                          <td colSpan={4} className="py-8 text-center text-muted-foreground">
                            {isNesting ? 'Finding markers...' : 'No markers yet. Start nesting to see results.'}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                  <div ref={resultsEndRef} />
                </div>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Other Fabrics Quick Access */}
        {order && (
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Other Fabrics in Order</CardTitle>
              <CardDescription>Quick access to nest other materials</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex gap-2 flex-wrap">
                {Array.from(new Set(order.order_lines.map(l => l.fabric_code)))
                  .filter(code => code !== fabricCode)
                  .map(code => (
                    <Link key={code} href={`/orders/${orderId}/nesting?fabric=${code}`}>
                      <Button variant="outline" size="sm">
                        <Layers className="mr-2 h-4 w-4" />
                        Nest {code}
                      </Button>
                    </Link>
                  ))
                }
                {Array.from(new Set(order.order_lines.map(l => l.fabric_code))).length === 1 && (
                  <span className="text-sm text-muted-foreground py-2">
                    This order only has one fabric ({fabricCode})
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </DashboardLayout>
  )
}
