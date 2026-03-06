'use client'

import { createContext, useContext, useEffect, useState, useCallback, ReactNode } from 'react'
import { api, Order, Pattern, NestingJob, Cutplan, Fabric, PatternPiece } from '@/lib/api'
import { useAuthStore } from '@/lib/auth-store'
import { useParams } from 'next/navigation'

interface OrderContextValue {
  // Data
  order: Order | null
  patterns: Pattern[]
  fabrics: Fabric[]
  nestingJobs: NestingJob[]
  cutplans: Cutplan[]
  patternPieces: Record<string, PatternPiece[]>

  // Loading
  isLoading: boolean
  orderId: string

  // Actions
  loadData: () => Promise<void>

  // Derived helpers
  currentPattern: Pattern | null
  orderFabricCodes: string[]
  orderSizes: string[]
  isConfigured: boolean
  hasNestingResults: boolean
  hasCutplans: boolean
  hasApprovedCutplan: boolean
  currentStep: number
  stepLabel: string
}

const OrderContext = createContext<OrderContextValue | null>(null)

export function useOrderContext() {
  const ctx = useContext(OrderContext)
  if (!ctx) throw new Error('useOrderContext must be used within an OrderProvider')
  return ctx
}

export function OrderProvider({ children }: { children: ReactNode }) {
  const params = useParams()
  const orderId = params.id as string
  const { isAuthenticated } = useAuthStore()

  const [order, setOrder] = useState<Order | null>(null)
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [fabrics, setFabrics] = useState<Fabric[]>([])
  const [nestingJobs, setNestingJobs] = useState<NestingJob[]>([])
  const [cutplans, setCutplans] = useState<Cutplan[]>([])
  const [patternPieces, setPatternPieces] = useState<Record<string, PatternPiece[]>>({})
  const [isLoading, setIsLoading] = useState(true)

  const loadData = useCallback(async () => {
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

      // Load pattern pieces if pattern is assigned
      if (orderData.pattern_id) {
        try {
          const piecesData = await api.getPatternPieces(orderData.pattern_id)
          setPatternPieces(piecesData.pieces_by_material || {})
        } catch (e) {
          console.log('Could not load pattern pieces:', e)
        }
      }
    } catch (error) {
      console.error('Failed to load order data:', error)
    } finally {
      setIsLoading(false)
    }
  }, [orderId])

  // Initial load
  useEffect(() => {
    if (isAuthenticated && orderId) {
      loadData()
    }
  }, [isAuthenticated, orderId, loadData])

  // Poll for nesting job progress when any job is running
  useEffect(() => {
    const hasRunningJob = nestingJobs.some(j => j.status === 'running')
    if (!hasRunningJob) return

    const interval = setInterval(async () => {
      try {
        const jobsData = await api.getNestingJobs(orderId)
        setNestingJobs(jobsData)
        // If job just completed, reload everything
        if (jobsData.every(j => j.status !== 'running') && jobsData.some(j => j.status === 'completed')) {
          loadData()
        }
      } catch (e) {
        console.error('Nesting polling error:', e)
      }
    }, 3000)

    return () => clearInterval(interval)
  }, [nestingJobs.some(j => j.status === 'running'), orderId, loadData])

  // Derived values
  const currentPattern = order?.pattern_id
    ? patterns.find(p => p.id === order.pattern_id) || null
    : null

  const orderFabricCodes = order
    ? Array.from(new Set(order.order_lines.map(line => line.fabric_code)))
    : []

  // Preserve size order from backend (which reflects Excel column order via sort_order)
  const orderSizesSet = new Set<string>()
  const orderSizes: string[] = []
  order?.order_lines.forEach(line => {
    line.size_quantities.forEach(sq => {
      if (!orderSizesSet.has(sq.size_code)) {
        orderSizesSet.add(sq.size_code)
        orderSizes.push(sq.size_code)
      }
    })
  })

  const orderFabricsInPattern = currentPattern
    ? orderFabricCodes.filter(code => currentPattern.available_materials.includes(code))
    : []

  const allConfigured = orderFabricsInPattern.every(code => {
    const fabric = fabrics.find(f => f.code === code)
    return !!fabric
  })

  const isConfigured = !!currentPattern && orderFabricsInPattern.length > 0 && allConfigured
  const hasNestingResults = nestingJobs.some(j => j.status === 'completed')
  const hasCutplans = cutplans.length > 0
  const hasApprovedCutplan = cutplans.some(
    c => c.status === 'approved' || c.status === 'refining' || c.status === 'refined'
  )

  // Current workflow step
  let currentStep = 1
  let stepLabel = 'Upload Order'

  if (order && order.order_lines.length > 0) {
    currentStep = 2
    stepLabel = 'Link Pattern'
  }
  if (order?.pattern_id) {
    currentStep = 3
    stepLabel = 'Configure Nesting'
  }
  if (isConfigured) {
    currentStep = 4
    stepLabel = 'Ready to Nest'
  }
  if (hasNestingResults) {
    currentStep = 5
    stepLabel = hasCutplans ? 'Review Cutplans' : 'Nesting Complete'
  }
  if (hasApprovedCutplan) {
    currentStep = 6
    const hasRefined = cutplans.some(c => c.status === 'refined')
    const isRefining = cutplans.some(c => c.status === 'refining')
    if (hasRefined) {
      stepLabel = 'Roll Plan'
    } else if (isRefining) {
      stepLabel = 'Refining...'
    } else {
      stepLabel = 'Ready to Refine'
    }
    // Step 7: Export Ready (after refinement complete)
    if (hasRefined) {
      currentStep = 7
      stepLabel = 'Export Ready'
    }
  }

  const value: OrderContextValue = {
    order,
    patterns,
    fabrics,
    nestingJobs,
    cutplans,
    patternPieces,
    isLoading,
    orderId,
    loadData,
    currentPattern,
    orderFabricCodes,
    orderSizes,
    isConfigured,
    hasNestingResults,
    hasCutplans,
    hasApprovedCutplan,
    currentStep,
    stepLabel,
  }

  return <OrderContext.Provider value={value}>{children}</OrderContext.Provider>
}
