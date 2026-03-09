'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { api, Fabric } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useToast } from '@/hooks/use-toast'
import { ArrowLeft, Plus, Trash2 } from 'lucide-react'

export default function FabricsPage() {
  const { toast } = useToast()
  const [fabrics, setFabrics] = useState<Fabric[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isAdding, setIsAdding] = useState(false)

  // Form state
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [widthInches, setWidthInches] = useState('')
  const [costPerYard, setCostPerYard] = useState('')

  useEffect(() => {
    loadFabrics()
  }, [])

  const loadFabrics = async () => {
    try {
      const data = await api.getFabrics()
      setFabrics(data)
    } catch (error) {
      toast({
        title: 'Failed to load fabrics',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsAdding(true)
    try {
      await api.createFabric({
        name,
        code,
        width_inches: parseFloat(widthInches),
        cost_per_yard: costPerYard ? parseFloat(costPerYard) : undefined,
      })
      toast({ title: 'Fabric added' })
      setName('')
      setCode('')
      setWidthInches('')
      setCostPerYard('')
      loadFabrics()
    } catch (error) {
      toast({
        title: 'Failed to add fabric',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsAdding(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deleteFabric(id)
      toast({ title: 'Fabric deleted' })
      loadFabrics()
    } catch (error) {
      toast({
        title: 'Failed to delete',
        variant: 'destructive',
      })
    }
  }

  return (
    <AuthGuard>
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex items-center space-x-4">
          <Link href="/settings">
            <Button variant="ghost" size="icon">
              <ArrowLeft className="h-4 w-4" />
            </Button>
          </Link>
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Fabrics</h1>
            <p className="text-muted-foreground">
              Manage your fabric library
            </p>
          </div>
        </div>

        <div className="grid gap-6 md:grid-cols-2">
          {/* Add Fabric Form */}
          <Card>
            <CardHeader>
              <CardTitle>Add Fabric</CardTitle>
              <CardDescription>Add a new fabric to your library</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="name">Name</Label>
                    <Input
                      id="name"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="e.g., Denim 10oz"
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="code">Code</Label>
                    <Input
                      id="code"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      placeholder="e.g., DEN-10"
                      required
                    />
                  </div>
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="width">Width (inches)</Label>
                    <Input
                      id="width"
                      type="number"
                      step="0.1"
                      value={widthInches}
                      onChange={(e) => setWidthInches(e.target.value)}
                      placeholder="e.g., 60"
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="cost">Cost per Yard ($)</Label>
                    <Input
                      id="cost"
                      type="number"
                      step="0.01"
                      value={costPerYard}
                      onChange={(e) => setCostPerYard(e.target.value)}
                      placeholder="e.g., 5.00"
                    />
                  </div>
                </div>
                <Button type="submit" disabled={isAdding}>
                  <Plus className="mr-2 h-4 w-4" />
                  {isAdding ? 'Adding...' : 'Add Fabric'}
                </Button>
              </form>
            </CardContent>
          </Card>

          {/* Fabric List */}
          <Card>
            <CardHeader>
              <CardTitle>Fabric Library</CardTitle>
              <CardDescription>{fabrics.length} fabrics</CardDescription>
            </CardHeader>
            <CardContent>
              {isLoading ? (
                <div className="flex justify-center py-8">
                  <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary"></div>
                </div>
              ) : fabrics.length === 0 ? (
                <p className="text-center text-muted-foreground py-8">
                  No fabrics added yet
                </p>
              ) : (
                <div className="space-y-3">
                  {fabrics.map((fabric) => (
                    <div
                      key={fabric.id}
                      className="flex items-center justify-between p-3 bg-gray-50 rounded-lg"
                    >
                      <div>
                        <p className="font-medium">{fabric.name}</p>
                        <p className="text-sm text-muted-foreground">
                          {fabric.code} | {fabric.width_inches}&quot; wide | ${fabric.cost_per_yard}/yd
                        </p>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleDelete(fabric.id)}
                      >
                        <Trash2 className="h-4 w-4 text-red-500" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </DashboardLayout>
    </AuthGuard>
  )
}
