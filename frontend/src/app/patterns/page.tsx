'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useAuthStore } from '@/lib/auth-store'
import { api, Pattern } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useToast } from '@/hooks/use-toast'
import { useDropzone } from 'react-dropzone'
import { Upload, Package, CheckCircle2, XCircle } from 'lucide-react'

export default function PatternsPage() {
  const router = useRouter()
  const { isAuthenticated, isLoading: authLoading, checkAuth } = useAuthStore()
  const { toast } = useToast()
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isUploading, setIsUploading] = useState(false)
  const [patternName, setPatternName] = useState('')
  const [dxfFile, setDxfFile] = useState<File | null>(null)
  const [rulFile, setRulFile] = useState<File | null>(null)

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.push('/login')
    }
  }, [authLoading, isAuthenticated, router])

  useEffect(() => {
    if (isAuthenticated) {
      loadPatterns()
    }
  }, [isAuthenticated])

  const loadPatterns = async () => {
    try {
      const data = await api.getPatterns()
      setPatterns(data)
    } catch (error) {
      toast({
        title: 'Failed to load patterns',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const onDropDxf = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      setDxfFile(acceptedFiles[0])
      if (!patternName) {
        setPatternName(acceptedFiles[0].name.replace(/\.dxf$/i, ''))
      }
    }
  }, [patternName])

  const onDropRul = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      setRulFile(acceptedFiles[0])
    }
  }, [])

  const dxfDropzone = useDropzone({
    onDrop: onDropDxf,
    accept: { 'application/dxf': ['.dxf'] },
    maxFiles: 1,
  })

  const rulDropzone = useDropzone({
    onDrop: onDropRul,
    accept: { 'text/plain': ['.rul'] },
    maxFiles: 1,
  })

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!dxfFile || !patternName) {
      toast({
        title: 'Missing required fields',
        description: 'Please provide a pattern name and DXF file',
        variant: 'destructive',
      })
      return
    }

    setIsUploading(true)
    try {
      const pattern = await api.uploadPattern(patternName, 'aama', dxfFile, rulFile || undefined)
      toast({
        title: 'Pattern uploaded',
        description: 'Now parsing pattern file...',
      })

      // Trigger parsing
      await api.parsePattern(pattern.id)
      toast({
        title: 'Pattern parsed',
        description: 'Pattern is ready to use',
      })

      // Reset form and reload
      setPatternName('')
      setDxfFile(null)
      setRulFile(null)
      loadPatterns()
    } catch (error) {
      toast({
        title: 'Upload failed',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsUploading(false)
    }
  }

  if (authLoading || !isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Patterns</h1>
          <p className="text-muted-foreground">
            Upload and manage pattern files for nesting
          </p>
        </div>

        <div className="grid gap-6 md:grid-cols-2">
          {/* Upload Form */}
          <Card>
            <CardHeader>
              <CardTitle>Upload Pattern</CardTitle>
              <CardDescription>
                Upload AAMA/ASTM DXF pattern files
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleUpload} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="patternName">Pattern Name</Label>
                  <Input
                    id="patternName"
                    value={patternName}
                    onChange={(e) => setPatternName(e.target.value)}
                    placeholder="Enter pattern name"
                    required
                  />
                </div>

                <div className="space-y-2">
                  <Label>DXF File (Required)</Label>
                  <div
                    {...dxfDropzone.getRootProps()}
                    className={`border-2 border-dashed rounded-lg p-4 text-center cursor-pointer ${
                      dxfDropzone.isDragActive ? 'border-primary bg-primary/5' : dxfFile ? 'border-green-500' : 'border-gray-300'
                    }`}
                  >
                    <input {...dxfDropzone.getInputProps()} />
                    {dxfFile ? (
                      <p className="text-sm text-green-600">{dxfFile.name}</p>
                    ) : (
                      <p className="text-sm text-muted-foreground">Drop .dxf file here</p>
                    )}
                  </div>
                </div>

                <div className="space-y-2">
                  <Label>RUL File (Optional)</Label>
                  <div
                    {...rulDropzone.getRootProps()}
                    className={`border-2 border-dashed rounded-lg p-4 text-center cursor-pointer ${
                      rulDropzone.isDragActive ? 'border-primary bg-primary/5' : rulFile ? 'border-green-500' : 'border-gray-300'
                    }`}
                  >
                    <input {...rulDropzone.getInputProps()} />
                    {rulFile ? (
                      <p className="text-sm text-green-600">{rulFile.name}</p>
                    ) : (
                      <p className="text-sm text-muted-foreground">Drop .rul file here</p>
                    )}
                  </div>
                </div>

                <Button type="submit" disabled={isUploading || !dxfFile} className="w-full">
                  {isUploading ? 'Uploading...' : 'Upload Pattern'}
                </Button>
              </form>
            </CardContent>
          </Card>

          {/* Pattern List */}
          <Card>
            <CardHeader>
              <CardTitle>Pattern Library</CardTitle>
              <CardDescription>
                {patterns.length} patterns available
              </CardDescription>
            </CardHeader>
            <CardContent>
              {isLoading ? (
                <div className="flex justify-center py-8">
                  <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary"></div>
                </div>
              ) : patterns.length === 0 ? (
                <p className="text-center text-muted-foreground py-8">
                  No patterns uploaded yet
                </p>
              ) : (
                <div className="space-y-3">
                  {patterns.map((pattern) => (
                    <div
                      key={pattern.id}
                      className="flex items-center justify-between p-3 bg-gray-50 rounded-lg"
                    >
                      <div className="flex items-center space-x-3">
                        <Package className="h-5 w-5 text-primary" />
                        <div>
                          <p className="font-medium">{pattern.name}</p>
                          <p className="text-xs text-muted-foreground">
                            {pattern.available_sizes.length > 0
                              ? `Sizes: ${pattern.available_sizes.join(', ')}`
                              : 'Not parsed'}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center">
                        {pattern.is_parsed ? (
                          <CheckCircle2 className="h-5 w-5 text-green-500" />
                        ) : (
                          <XCircle className="h-5 w-5 text-gray-400" />
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </DashboardLayout>
  )
}
