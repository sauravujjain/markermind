'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { api, Order } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useToast } from '@/hooks/use-toast'
import { Plus, Search, FileText, Clock, CheckCircle2, AlertCircle, ChevronRight, Loader2, Trash2 } from 'lucide-react'

const statusConfig: Record<string, { bg: string; text: string; dot: string }> = {
  draft: { bg: 'bg-muted', text: 'text-muted-foreground', dot: 'bg-muted-foreground' },
  pending_pattern: { bg: 'bg-amber-100/80', text: 'text-amber-700', dot: 'bg-amber-500' },
  pending_nesting: { bg: 'bg-blue-100/80', text: 'text-blue-700', dot: 'bg-blue-500' },
  nesting_in_progress: { bg: 'bg-purple-100/80', text: 'text-purple-700', dot: 'bg-purple-500 animate-pulse' },
  pending_cutplan: { bg: 'bg-accent/10', text: 'text-accent', dot: 'bg-accent' },
  cutplan_ready: { bg: 'bg-green-100/80', text: 'text-green-700', dot: 'bg-green-500' },
  approved: { bg: 'bg-emerald-100/80', text: 'text-emerald-700', dot: 'bg-emerald-500' },
  completed: { bg: 'bg-secondary', text: 'text-secondary-foreground', dot: 'bg-secondary-foreground/50' },
}

const statusLabels: Record<string, string> = {
  draft: 'Step 1: Order Created',
  pending_pattern: 'Step 2: Link Pattern',
  pending_nesting: 'Step 3: Configure',
  nesting_in_progress: 'Step 4: Nesting...',
  pending_cutplan: 'Step 4: Nesting Done',
  cutplan_ready: 'Step 5: Cutplans Ready',
  approved: 'Approved',
  completed: 'Completed',
}

const statusSteps: Record<string, number> = {
  draft: 1,
  pending_pattern: 2,
  pending_nesting: 3,
  nesting_in_progress: 4,
  pending_cutplan: 4,
  cutplan_ready: 5,
  approved: 6,
  completed: 6,
}

export default function OrdersPage() {
  const { toast } = useToast()
  const [orders, setOrders] = useState<Order[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [selectedOrders, setSelectedOrders] = useState<Set<string>>(new Set())
  const [isDeleting, setIsDeleting] = useState(false)

  useEffect(() => {
    loadOrders()
  }, [])

  const loadOrders = async () => {
    try {
      const data = await api.getOrders()
      setOrders(data)
    } catch (error) {
      toast({
        title: 'Failed to load orders',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const filteredOrders = orders
    .filter((order) => order.order_number.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())

  const toggleOrderSelection = (orderId: string, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setSelectedOrders(prev => {
      const newSet = new Set(prev)
      if (newSet.has(orderId)) {
        newSet.delete(orderId)
      } else {
        newSet.add(orderId)
      }
      return newSet
    })
  }

  const toggleSelectAll = () => {
    if (selectedOrders.size === filteredOrders.length) {
      setSelectedOrders(new Set())
    } else {
      setSelectedOrders(new Set(filteredOrders.map(o => o.id)))
    }
  }

  const handleDeleteSelected = async () => {
    if (selectedOrders.size === 0) return

    const confirmed = window.confirm(`Are you sure you want to delete ${selectedOrders.size} order(s)? This action cannot be undone.`)
    if (!confirmed) return

    setIsDeleting(true)
    try {
      const deletePromises = Array.from(selectedOrders).map(id => api.deleteOrder(id))
      await Promise.all(deletePromises)

      toast({
        title: 'Orders deleted',
        description: `Successfully deleted ${selectedOrders.size} order(s)`,
      })

      setSelectedOrders(new Set())
      loadOrders()
    } catch (error) {
      toast({
        title: 'Failed to delete orders',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsDeleting(false)
    }
  }

  return (
    <AuthGuard>
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-foreground">Orders</h1>
            <p className="text-muted-foreground mt-1">
              Manage your cutting orders and cutplans
            </p>
          </div>
          <Link href="/orders/new">
            <Button className="shadow-warm hover:shadow-warm-lg transition-all duration-200 hover:-translate-y-0.5">
              <Plus className="mr-2 h-4 w-4" />
              New Order
            </Button>
          </Link>
        </div>

        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="relative flex-1 max-w-md">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Search orders..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-10 h-11 bg-card border-border/50 focus:border-primary focus:ring-primary/20"
              />
            </div>
            {filteredOrders.length > 0 && (
              <label className="flex items-center gap-2 cursor-pointer text-sm text-muted-foreground hover:text-foreground transition-colors">
                <input
                  type="checkbox"
                  checked={selectedOrders.size === filteredOrders.length && filteredOrders.length > 0}
                  onChange={toggleSelectAll}
                  className="w-4 h-4 rounded border-gray-300 text-primary focus:ring-primary"
                />
                Select All
              </label>
            )}
          </div>
          {selectedOrders.size > 0 && (
            <Button
              variant="destructive"
              size="sm"
              onClick={handleDeleteSelected}
              disabled={isDeleting}
              className="shadow-sm"
            >
              {isDeleting ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="mr-2 h-4 w-4" />
              )}
              Delete ({selectedOrders.size})
            </Button>
          )}
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-16">
            <div className="flex flex-col items-center gap-3">
              <Loader2 className="w-8 h-8 text-primary animate-spin" />
              <p className="text-sm text-muted-foreground">Loading orders...</p>
            </div>
          </div>
        ) : filteredOrders.length === 0 ? (
          <Card className="border-border/50 border-dashed">
            <CardContent className="flex flex-col items-center justify-center py-16">
              <div className="w-16 h-16 rounded-2xl bg-muted flex items-center justify-center mb-4">
                <FileText className="h-8 w-8 text-muted-foreground" />
              </div>
              <h3 className="text-lg font-semibold text-foreground">No orders found</h3>
              <p className="text-muted-foreground mb-6 text-center max-w-sm">
                {search ? 'Try a different search term' : 'Get started by creating your first order'}
              </p>
              {!search && (
                <Link href="/orders/new">
                  <Button className="shadow-warm">
                    <Plus className="mr-2 h-4 w-4" />
                    Create Order
                  </Button>
                </Link>
              )}
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-3">
            {filteredOrders.map((order, index) => {
              const status = statusConfig[order.status] || statusConfig.draft
              const isSelected = selectedOrders.has(order.id)
              return (
                <div key={order.id} className="relative">
                  <Link href={`/orders/${order.id}`}>
                    <Card
                      className={`card-hover border-border/50 cursor-pointer group ${isSelected ? 'ring-2 ring-primary border-primary' : ''}`}
                      style={{ animationDelay: `${index * 50}ms` }}
                    >
                      <CardContent className="p-5">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-4">
                            {/* Selection checkbox */}
                            <div
                              onClick={(e) => toggleOrderSelection(order.id, e)}
                              className="flex items-center justify-center"
                            >
                              <input
                                type="checkbox"
                                checked={isSelected}
                                onChange={() => {}}
                                className="w-4 h-4 rounded border-gray-300 text-primary focus:ring-primary cursor-pointer"
                              />
                            </div>
                            <div className="w-12 h-12 bg-gradient-to-br from-primary/10 to-accent/10 rounded-xl flex items-center justify-center group-hover:scale-105 transition-transform duration-200">
                              <FileText className="h-5 w-5 text-primary" />
                            </div>
                          <div>
                            <h3 className="font-semibold text-foreground group-hover:text-primary transition-colors">
                              {order.order_number}
                              {order.notes && (
                                <span className="ml-2 text-xs font-normal text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                                  {order.notes}
                                </span>
                              )}
                            </h3>
                            <p className="text-sm text-muted-foreground">
                              <span className="text-accent font-medium">{order.order_lines?.length || 0}</span> lines, {' '}
                              <span className="font-medium">{(order.order_lines || []).reduce((acc, c) => acc + c.size_quantities.reduce((a, s) => a + s.quantity, 0), 0).toLocaleString()}</span> units
                            </p>
                          </div>
                        </div>
                        <div className="flex items-center gap-4">
                          {/* Step progress indicator */}
                          <div className="hidden sm:flex items-center gap-1">
                            {[1, 2, 3, 4, 5, 6].map((step) => {
                              const currentStep = statusSteps[order.status] || 1
                              const isCompleted = step < currentStep
                              const isCurrent = step === currentStep
                              return (
                                <div
                                  key={step}
                                  className={`w-2 h-2 rounded-full transition-all ${
                                    isCompleted
                                      ? 'bg-green-500'
                                      : isCurrent
                                      ? order.status === 'nesting_in_progress'
                                        ? 'bg-purple-500 animate-pulse'
                                        : 'bg-primary'
                                      : 'bg-muted'
                                  }`}
                                  title={`Step ${step}`}
                                />
                              )
                            })}
                          </div>
                          <span
                            className={`px-3 py-1.5 rounded-full text-xs font-medium flex items-center gap-1.5 ${status.bg} ${status.text}`}
                          >
                            <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
                            {statusLabels[order.status] || order.status}
                          </span>
                          <span className="text-sm text-muted-foreground hidden sm:block">
                            {new Date(order.created_at).toLocaleDateString()} {new Date(order.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                          </span>
                          <ChevronRight className="h-4 w-4 text-muted-foreground group-hover:text-primary group-hover:translate-x-1 transition-all" />
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                </Link>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </DashboardLayout>
    </AuthGuard>
  )
}
