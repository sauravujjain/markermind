'use client'

import { useRouter, usePathname } from 'next/navigation'
import Link from 'next/link'
import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { OrderProvider, useOrderContext } from './order-context'
import {
  ArrowLeft,
  Play,
  CheckCircle2,
  Clock,
  Settings,
  Loader2,
} from 'lucide-react'

function OrderLayoutInner({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const pathname = usePathname()
  const {
    order,
    orderId,
    isLoading,
    patterns,
    fabrics,
    nestingJobs,
    cutplans,
    currentPattern,
    orderFabricCodes,
    isConfigured,
    hasNestingResults,
    hasCutplans,
    hasApprovedCutplan,
    currentStep,
    stepLabel,
  } = useOrderContext()

  const totalUnits = order
    ? order.order_lines.reduce(
        (acc, c) => acc + c.size_quantities.reduce((a, s) => a + s.quantity, 0),
        0
      )
    : 0

  const isNesting = nestingJobs.some(j => j.status === 'running')

  if (isLoading) {
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

  // Determine which sub-page we're on for highlighting
  const isOnMain = pathname === `/orders/${orderId}`
  const isOnConfigure = pathname === `/orders/${orderId}/configure`
  const isOnNesting = pathname === `/orders/${orderId}/nesting`
  const isOnCutplan = pathname === `/orders/${orderId}/cutplan`
  const isOnRollplan = pathname === `/orders/${orderId}/rollplan`
  const isOnExport = pathname === `/orders/${orderId}/export`

  // Determine next action for sticky bar
  let nextAction: { label: string; onClick: () => void; disabled?: boolean } | null = null

  if (isConfigured && !hasNestingResults) {
    nextAction = {
      label: isNesting ? 'GPU Nesting in Progress...' : 'Configure & Nest',
      onClick: () => router.push(`/orders/${orderId}/configure`),
      disabled: isNesting,
    }
  } else if (hasNestingResults && !hasCutplans) {
    nextAction = {
      label: 'Generate Cutplan Options',
      onClick: () => router.push(`/orders/${orderId}/cutplan`),
    }
  } else if (hasApprovedCutplan) {
    const hasRefined = cutplans.some(c => c.status === 'refined')
    const isRefining = cutplans.some(c => c.status === 'refining')
    if (hasRefined) {
      nextAction = {
        label: 'View Reports',
        onClick: () => router.push(`/orders/${orderId}/export`),
      }
    } else if (!isRefining) {
      nextAction = {
        label: 'View Cutplans',
        onClick: () => router.push(`/orders/${orderId}/cutplan`),
      }
    }
  } else if (!order.pattern_id) {
    // No pattern yet — stay on main page
    nextAction = null
  } else if (!isConfigured) {
    nextAction = {
      label: 'Configure GPU Nesting',
      onClick: () => router.push(`/orders/${orderId}/configure`),
    }
  }

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
            <Button
              variant="outline"
              onClick={() => router.push(`/orders/${orderId}/configure`)}
              disabled={!order.pattern_id}
              title={!order.pattern_id ? 'Select a pattern first' : 'Configure & run GPU nesting'}
            >
              <Settings className="mr-2 h-4 w-4" />
              Configure & Nest
            </Button>
            <Button
              onClick={() => router.push(`/orders/${orderId}/cutplan`)}
              disabled={!hasNestingResults}
              title={!hasNestingResults ? 'Run GPU nesting first' : 'Generate cutplan options'}
            >
              Generate Cutplan
            </Button>
          </div>
        </div>

        {/* Workflow Steps */}
        <div className="grid gap-3 md:grid-cols-7">
          <Link href={`/orders/${orderId}`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${order.order_lines.length > 0 ? 'border-green-500' : ''} ${isOnMain ? 'ring-2 ring-primary/30' : ''}`}>
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
          </Link>

          <Link href={`/orders/${orderId}`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${order.pattern_id ? 'border-green-500' : ''} ${isOnMain ? 'ring-2 ring-primary/30' : ''}`}>
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
          </Link>

          <Link href={`/orders/${orderId}/configure`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${isConfigured ? 'border-green-500' : order.pattern_id ? 'border-amber-500' : ''} ${isOnConfigure ? 'ring-2 ring-primary/30' : ''}`}>
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
          </Link>

          <Link href={`/orders/${orderId}/nesting`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${nestingJobs.some(j => j.status === 'completed') ? 'border-green-500' : ''} ${isOnNesting ? 'ring-2 ring-primary/30' : ''}`}>
              <CardHeader className="pb-2">
                <div className="flex items-center space-x-2">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${nestingJobs.some(j => j.status === 'completed') ? 'bg-green-100 text-green-600' : nestingJobs.some(j => j.status === 'running') ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-400'}`}>
                    {nestingJobs.some(j => j.status === 'completed') ? <CheckCircle2 className="h-3.5 w-3.5" /> : nestingJobs.some(j => j.status === 'running') ? <Clock className="h-3.5 w-3.5 animate-pulse" /> : '4'}
                  </div>
                  <CardTitle className="text-xs">GPU Nesting</CardTitle>
                </div>
              </CardHeader>
              <CardContent className="pt-0">
                <p className="text-xs text-muted-foreground">
                  {nestingJobs.some(j => j.status === 'completed') ? 'Complete' : nestingJobs.some(j => j.status === 'running') ? 'Running...' : 'GPU nesting'}
                </p>
              </CardContent>
            </Card>
          </Link>

          <Link href={`/orders/${orderId}/cutplan`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${cutplans.some(c => c.status === 'ready' || c.status === 'approved') ? 'border-green-500' : ''} ${isOnCutplan ? 'ring-2 ring-primary/30' : ''}`}>
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
          </Link>

          <Link href={`/orders/${orderId}/rollplan`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${hasApprovedCutplan && cutplans.some(c => c.status === 'refined') ? 'border-amber-500' : ''} ${isOnRollplan ? 'ring-2 ring-primary/30' : ''}`}>
              <CardHeader className="pb-2">
                <div className="flex items-center space-x-2">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${hasApprovedCutplan && cutplans.some(c => c.status === 'refined') ? 'bg-amber-100 text-amber-600' : 'bg-gray-100 text-gray-400'}`}>
                    {'6'}
                  </div>
                  <CardTitle className="text-xs">Roll Plan</CardTitle>
                </div>
              </CardHeader>
              <CardContent className="pt-0">
                <p className="text-xs text-muted-foreground">
                  {cutplans.some(c => c.status === 'refined') ? 'Plan rolls' : 'Pending'}
                </p>
              </CardContent>
            </Card>
          </Link>

          <Link href={`/orders/${orderId}/export`}>
            <Card className={`cursor-pointer transition-all hover:shadow-md ${cutplans.some(c => c.status === 'refined') ? 'border-green-500' : cutplans.some(c => c.status === 'refining') ? 'border-blue-500' : cutplans.some(c => c.status === 'approved') ? 'border-amber-500' : ''} ${isOnExport ? 'ring-2 ring-primary/30' : ''}`}>
              <CardHeader className="pb-2">
                <div className="flex items-center space-x-2">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs ${cutplans.some(c => c.status === 'refined') ? 'bg-green-100 text-green-600' : cutplans.some(c => c.status === 'refining') ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-400'}`}>
                    {cutplans.some(c => c.status === 'refined') ? <CheckCircle2 className="h-3.5 w-3.5" /> : cutplans.some(c => c.status === 'refining') ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '7'}
                  </div>
                  <CardTitle className="text-xs">Export</CardTitle>
                </div>
              </CardHeader>
              <CardContent className="pt-0">
                <p className="text-xs text-muted-foreground">
                  {cutplans.some(c => c.status === 'refined') ? 'Ready' : cutplans.some(c => c.status === 'refining') ? 'Nesting...' : cutplans.some(c => c.status === 'approved') ? 'Refine' : 'Dockets'}
                </p>
              </CardContent>
            </Card>
          </Link>
        </div>

        {/* Page Content */}
        {children}

        {/* Spacer for sticky action bar */}
        <div className="h-20" />
      </div>

      {/* Sticky Action Bar */}
      <div className="fixed bottom-0 left-0 right-0 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 border-t z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3">
          <div className="flex items-center justify-between">
            {/* Progress indicator */}
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-1">
                {[1, 2, 3, 4, 5, 6, 7].map((step) => (
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
                  onClick={() => router.push(`/orders/${orderId}/configure`)}
                  title="Re-run GPU nesting with different parameters"
                >
                  Re-run GPU Nesting
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </DashboardLayout>
  )
}

export default function OrderLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      <OrderProvider>
        <OrderLayoutInner>{children}</OrderLayoutInner>
      </OrderProvider>
    </AuthGuard>
  )
}
