'use client'

import { useEffect, useState } from 'react'
import { useRouter, useParams } from 'next/navigation'
import Link from 'next/link'
import { useAuthStore } from '@/lib/auth-store'
import { api, Order, Pattern, NestingJob, Cutplan } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { ArrowLeft, Play, FileText, CheckCircle2, Clock, Package, ChevronDown } from 'lucide-react'
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
  const [nestingJobs, setNestingJobs] = useState<NestingJob[]>([])
  const [cutplans, setCutplans] = useState<Cutplan[]>([])
  const [isLoading, setIsLoading] = useState(true)

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

  const loadData = async () => {
    try {
      const [orderData, patternsData, jobsData, cutplansData] = await Promise.all([
        api.getOrder(orderId),
        api.getPatterns(),
        api.getNestingJobs(orderId),
        api.getCutplans(orderId),
      ])
      setOrder(orderData)
      setPatterns(patternsData)
      setNestingJobs(jobsData)
      setCutplans(cutplansData)
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

  const handleStartNesting = async () => {
    if (!order?.pattern_id) {
      toast({
        title: 'No pattern selected',
        description: 'Please select a pattern before starting nesting',
        variant: 'destructive',
      })
      return
    }

    // Get fabric width from pattern
    const pattern = patterns.find(p => p.id === order.pattern_id)
    if (!pattern) {
      toast({
        title: 'Pattern not found',
        description: 'Please select a valid pattern',
        variant: 'destructive',
      })
      return
    }

    try {
      const job = await api.createNestingJob({
        order_id: orderId,
        pattern_id: order.pattern_id,
        fabric_width_inches: 60, // Default, should come from fabric
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
    try {
      const plans = await api.optimizeCutplan({
        order_id: orderId,
        generate_options: ['balanced'],
      })
      toast({
        title: 'Cutplan optimization complete',
        description: `Generated ${plans.length} cutplan options`,
      })
      loadData() // Reload to show new cutplans
    } catch (error) {
      toast({
        title: 'Failed to optimize cutplan',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    }
  }

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
          <div className="flex space-x-2">
            <Button variant="outline" onClick={handleStartNesting}>
              <Play className="mr-2 h-4 w-4" />
              Run Nesting
            </Button>
            <Button onClick={handleOptimizeCutplan}>
              Generate Cutplan
            </Button>
          </div>
        </div>

        {/* Workflow Steps */}
        <div className="grid gap-4 md:grid-cols-5">
          <Card className={order.status !== 'draft' ? 'border-green-500' : ''}>
            <CardHeader className="pb-2">
              <div className="flex items-center space-x-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${order.order_lines.length > 0 ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                  {order.order_lines.length > 0 ? <CheckCircle2 className="h-4 w-4" /> : '1'}
                </div>
                <CardTitle className="text-sm">Order Entry</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                {order.order_lines.length > 0 ? `${order.order_lines.length} colors added` : 'Add order quantities'}
              </p>
            </CardContent>
          </Card>

          <Card className={order.pattern_id ? 'border-green-500' : ''}>
            <CardHeader className="pb-2">
              <div className="flex items-center space-x-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${order.pattern_id ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                  {order.pattern_id ? <CheckCircle2 className="h-4 w-4" /> : '2'}
                </div>
                <CardTitle className="text-sm">Pattern</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                {order.pattern_id ? 'Pattern assigned' : 'Assign pattern'}
              </p>
            </CardContent>
          </Card>

          <Card className={nestingJobs.some(j => j.status === 'completed') ? 'border-green-500' : ''}>
            <CardHeader className="pb-2">
              <div className="flex items-center space-x-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${nestingJobs.some(j => j.status === 'completed') ? 'bg-green-100 text-green-600' : nestingJobs.some(j => j.status === 'running') ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-400'}`}>
                  {nestingJobs.some(j => j.status === 'completed') ? <CheckCircle2 className="h-4 w-4" /> : nestingJobs.some(j => j.status === 'running') ? <Clock className="h-4 w-4 animate-pulse" /> : '3'}
                </div>
                <CardTitle className="text-sm">Nesting</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                {nestingJobs.some(j => j.status === 'completed') ? 'Nesting complete' : nestingJobs.some(j => j.status === 'running') ? 'Running...' : 'Run GPU nesting'}
              </p>
            </CardContent>
          </Card>

          <Card className={cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? 'border-green-500' : ''}>
            <CardHeader className="pb-2">
              <div className="flex items-center space-x-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                  {cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? <CheckCircle2 className="h-4 w-4" /> : '4'}
                </div>
                <CardTitle className="text-sm">Cutplan</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                {cutplans.some(c => c.status === 'approved') ? 'Approved' : cutplans.length > 0 ? 'Review options' : 'Generate cutplan'}
              </p>
            </CardContent>
          </Card>

          <Card className={order.status === 'completed' ? 'border-green-500' : ''}>
            <CardHeader className="pb-2">
              <div className="flex items-center space-x-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${order.status === 'completed' ? 'bg-green-100 text-green-600' : 'bg-gray-100 text-gray-400'}`}>
                  {order.status === 'completed' ? <CheckCircle2 className="h-4 w-4" /> : '5'}
                </div>
                <CardTitle className="text-sm">Export</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                {order.status === 'completed' ? 'Exported' : 'Export dockets'}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Order Details */}
        <div className="grid gap-6 md:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Order Quantities</CardTitle>
              <CardDescription>Size breakdown by color</CardDescription>
            </CardHeader>
            <CardContent>
              {order.order_lines.length === 0 ? (
                <p className="text-muted-foreground">No quantities added yet</p>
              ) : (
                <div className="space-y-4">
                  {order.order_lines.map((color) => (
                    <div key={color.id}>
                      <h4 className="font-medium mb-2">{color.color_code}</h4>
                      <div className="flex flex-wrap gap-2">
                        {color.size_quantities.map((sq) => (
                          <div key={sq.id} className="bg-gray-100 rounded px-2 py-1 text-sm">
                            {sq.size_code}: {sq.quantity}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Pattern</CardTitle>
              <CardDescription>Select a pattern for this order</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                <Select
                  value={order.pattern_id || ''}
                  onValueChange={async (value) => {
                    try {
                      await api.updateOrder(orderId, { pattern_id: value || null })
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

                {order.pattern_id && (
                  <div className="p-3 bg-gray-50 rounded-lg">
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

        {/* Cutplans */}
        {cutplans.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>Cutplan Options</CardTitle>
              <CardDescription>Compare and select the best cutplan</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {cutplans.map((plan) => (
                  <Card key={plan.id} className="border-2">
                    <CardHeader className="pb-2">
                      <CardTitle className="text-lg">{plan.name || 'Cutplan'}</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <div className="space-y-2 text-sm">
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Efficiency</span>
                          <span className="font-medium">{plan.efficiency?.toFixed(1)}%</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Total Yards</span>
                          <span className="font-medium">{plan.total_yards?.toFixed(1)}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Markers</span>
                          <span className="font-medium">{plan.unique_markers}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Total Cost</span>
                          <span className="font-medium">${plan.total_cost?.toFixed(2)}</span>
                        </div>
                      </div>
                      <div className="mt-4 flex space-x-2">
                        {plan.status === 'approved' ? (
                          <Button className="w-full" variant="outline" disabled>
                            Approved
                          </Button>
                        ) : (
                          <Button
                            className="w-full"
                            onClick={async () => {
                              await api.approveCutplan(plan.id)
                              loadData()
                            }}
                          >
                            Approve
                          </Button>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </DashboardLayout>
  )
}
