'use client'

import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { FileText, Package, Scissors, TrendingUp, Plus, Upload, ArrowRight, CheckCircle2, Loader2, Clock } from 'lucide-react'
import Link from 'next/link'

export default function DashboardPage() {
  return (
    <AuthGuard>
    <DashboardLayout>
      <div className="space-y-8">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-foreground">Dashboard</h1>
            <p className="text-muted-foreground mt-1">
              Welcome to <span className="text-primary font-medium">MarkerMind</span> - your cutting optimization platform
            </p>
          </div>
          <Link href="/orders/new">
            <Button className="shadow-warm hover:shadow-warm-lg transition-all duration-200 hover:-translate-y-0.5">
              <Plus className="h-4 w-4 mr-2" />
              New Order
            </Button>
          </Link>
        </div>

        {/* Quick Stats */}
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Card className="card-hover border-border/50 overflow-hidden">
            <div className="absolute top-0 right-0 w-20 h-20 bg-primary/5 rounded-full -mr-10 -mt-10" />
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Active Orders</CardTitle>
              <div className="h-9 w-9 rounded-xl bg-primary/10 flex items-center justify-center">
                <FileText className="h-4 w-4 text-primary" />
              </div>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-foreground">12</div>
              <p className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
                <span className="inline-flex h-2 w-2 rounded-full bg-accent animate-pulse" />
                3 pending nesting
              </p>
            </CardContent>
          </Card>

          <Card className="card-hover border-border/50 overflow-hidden">
            <div className="absolute top-0 right-0 w-20 h-20 bg-secondary/20 rounded-full -mr-10 -mt-10" />
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Patterns</CardTitle>
              <div className="h-9 w-9 rounded-xl bg-secondary flex items-center justify-center">
                <Package className="h-4 w-4 text-accent" />
              </div>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-foreground">24</div>
              <p className="text-xs text-muted-foreground mt-1">
                <span className="text-accent font-medium">+5</span> new this month
              </p>
            </CardContent>
          </Card>

          <Card className="card-hover border-border/50 overflow-hidden">
            <div className="absolute top-0 right-0 w-20 h-20 bg-green-500/5 rounded-full -mr-10 -mt-10" />
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Avg Efficiency</CardTitle>
              <div className="h-9 w-9 rounded-xl bg-green-500/10 flex items-center justify-center">
                <TrendingUp className="h-4 w-4 text-green-600" />
              </div>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-foreground">82.4%</div>
              <p className="text-xs mt-1">
                <span className="text-green-600 font-medium">+2.1%</span>
                <span className="text-muted-foreground"> from last month</span>
              </p>
            </CardContent>
          </Card>

          <Card className="card-hover border-border/50 overflow-hidden">
            <div className="absolute top-0 right-0 w-20 h-20 bg-accent/10 rounded-full -mr-10 -mt-10" />
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Approved Plans</CardTitle>
              <div className="h-9 w-9 rounded-xl bg-accent/10 flex items-center justify-center">
                <Scissors className="h-4 w-4 text-accent" />
              </div>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-foreground">8</div>
              <p className="text-xs text-muted-foreground mt-1">
                This week
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Quick Actions */}
        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          <Card className="card-hover border-border/50 group">
            <CardHeader>
              <div className="h-12 w-12 rounded-2xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-warm mb-3 group-hover:scale-110 transition-transform duration-300">
                <Plus className="h-6 w-6 text-primary-foreground" />
              </div>
              <CardTitle className="text-lg">New Order</CardTitle>
              <CardDescription>
                Create a new order from Excel/CSV or enter manually
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/orders/new">
                <Button className="w-full group/btn">
                  Create Order
                  <ArrowRight className="h-4 w-4 ml-2 group-hover/btn:translate-x-1 transition-transform" />
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card className="card-hover border-border/50 group">
            <CardHeader>
              <div className="h-12 w-12 rounded-2xl bg-gradient-to-br from-accent to-accent/70 flex items-center justify-center shadow-warm mb-3 group-hover:scale-110 transition-transform duration-300">
                <Upload className="h-6 w-6 text-accent-foreground" />
              </div>
              <CardTitle className="text-lg">Upload Pattern</CardTitle>
              <CardDescription>
                Upload DXF/AAMA pattern files for nesting
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/patterns">
                <Button variant="outline" className="w-full border-accent/30 text-accent hover:bg-accent hover:text-accent-foreground group/btn">
                  Upload Pattern
                  <ArrowRight className="h-4 w-4 ml-2 group-hover/btn:translate-x-1 transition-transform" />
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card className="card-hover border-border/50 group">
            <CardHeader>
              <div className="h-12 w-12 rounded-2xl bg-gradient-to-br from-secondary to-secondary/70 flex items-center justify-center shadow-warm mb-3 group-hover:scale-110 transition-transform duration-300">
                <FileText className="h-6 w-6 text-accent" />
              </div>
              <CardTitle className="text-lg">View Orders</CardTitle>
              <CardDescription>
                Manage existing orders and cutplans
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/orders">
                <Button variant="outline" className="w-full group/btn">
                  View Orders
                  <ArrowRight className="h-4 w-4 ml-2 group-hover/btn:translate-x-1 transition-transform" />
                </Button>
              </Link>
            </CardContent>
          </Card>
        </div>

        {/* Recent Activity */}
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle className="text-lg">Recent Activity</CardTitle>
              <CardDescription>
                Your latest actions and updates
              </CardDescription>
            </div>
            <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
              View all
              <ArrowRight className="h-3 w-3 ml-1" />
            </Button>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="flex items-start gap-4 p-3 rounded-xl hover:bg-muted/50 transition-colors">
                <div className="h-10 w-10 rounded-full bg-green-500/10 flex items-center justify-center flex-shrink-0">
                  <CheckCircle2 className="h-5 w-5 text-green-600" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-foreground">
                    Cutplan approved for Order #1234
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    2 hours ago
                  </p>
                </div>
                <span className="text-xs px-2 py-1 rounded-full bg-green-500/10 text-green-600 font-medium">
                  Approved
                </span>
              </div>

              <div className="flex items-start gap-4 p-3 rounded-xl hover:bg-muted/50 transition-colors">
                <div className="h-10 w-10 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0">
                  <Loader2 className="h-5 w-5 text-primary" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-foreground">
                    Nesting completed for Order #1233
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    5 hours ago
                  </p>
                </div>
                <span className="text-xs px-2 py-1 rounded-full bg-primary/10 text-primary font-medium">
                  Completed
                </span>
              </div>

              <div className="flex items-start gap-4 p-3 rounded-xl hover:bg-muted/50 transition-colors">
                <div className="h-10 w-10 rounded-full bg-accent/10 flex items-center justify-center flex-shrink-0">
                  <Clock className="h-5 w-5 text-accent" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-foreground">
                    New order created: #1235
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Yesterday
                  </p>
                </div>
                <span className="text-xs px-2 py-1 rounded-full bg-accent/10 text-accent font-medium">
                  New
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </DashboardLayout>
    </AuthGuard>
  )
}
