'use client'

import { useEffect, useState, useCallback } from 'react'
import { api, Pattern } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useToast } from '@/hooks/use-toast'
import { useDropzone } from 'react-dropzone'
import { Upload, Package, CheckCircle2, XCircle, FileUp, Loader2, File } from 'lucide-react'

export default function PatternsPage() {
  const { toast } = useToast()
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isUploading, setIsUploading] = useState(false)
  const [patternName, setPatternName] = useState('')
  const [dxfFile, setDxfFile] = useState<File | null>(null)
  const [rulFile, setRulFile] = useState<File | null>(null)

  useEffect(() => {
    loadPatterns()
  }, [])

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

  return (
    <AuthGuard>
    <DashboardLayout>
      <div className="space-y-8">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-foreground">Patterns</h1>
          <p className="text-muted-foreground mt-1">
            Upload and manage pattern files for nesting
          </p>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          {/* Upload Form */}
          <Card className="border-border/50 shadow-warm">
            <CardHeader>
              <div className="flex items-center gap-3">
                <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-accent to-accent/70 flex items-center justify-center">
                  <FileUp className="h-5 w-5 text-accent-foreground" />
                </div>
                <div>
                  <CardTitle className="text-lg">Upload Pattern</CardTitle>
                  <CardDescription>
                    Upload AAMA/ASTM DXF pattern files
                  </CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleUpload} className="space-y-5">
                <div className="space-y-2">
                  <Label htmlFor="patternName" className="text-foreground font-medium">Pattern Name</Label>
                  <Input
                    id="patternName"
                    value={patternName}
                    onChange={(e) => setPatternName(e.target.value)}
                    placeholder="Enter pattern name"
                    required
                    className="h-11 bg-muted/50 border-border focus:border-primary"
                  />
                </div>

                <div className="space-y-2">
                  <Label className="text-foreground font-medium">DXF File <span className="text-primary">(Required)</span></Label>
                  <div
                    {...dxfDropzone.getRootProps()}
                    className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-all duration-200 ${
                      dxfDropzone.isDragActive
                        ? 'border-primary bg-primary/5 scale-[1.02]'
                        : dxfFile
                        ? 'border-green-500 bg-green-50/50'
                        : 'border-border hover:border-primary/50 hover:bg-muted/50'
                    }`}
                  >
                    <input {...dxfDropzone.getInputProps()} />
                    {dxfFile ? (
                      <div className="flex items-center justify-center gap-2">
                        <CheckCircle2 className="h-5 w-5 text-green-600" />
                        <p className="text-sm font-medium text-green-700">{dxfFile.name}</p>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <Upload className="h-8 w-8 mx-auto text-muted-foreground" />
                        <p className="text-sm text-muted-foreground">Drop .dxf file here or click to browse</p>
                      </div>
                    )}
                  </div>
                </div>

                <div className="space-y-2">
                  <Label className="text-foreground font-medium">RUL File <span className="text-muted-foreground font-normal">(Optional)</span></Label>
                  <div
                    {...rulDropzone.getRootProps()}
                    className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-all duration-200 ${
                      rulDropzone.isDragActive
                        ? 'border-accent bg-accent/5 scale-[1.02]'
                        : rulFile
                        ? 'border-green-500 bg-green-50/50'
                        : 'border-border hover:border-accent/50 hover:bg-muted/50'
                    }`}
                  >
                    <input {...rulDropzone.getInputProps()} />
                    {rulFile ? (
                      <div className="flex items-center justify-center gap-2">
                        <CheckCircle2 className="h-5 w-5 text-green-600" />
                        <p className="text-sm font-medium text-green-700">{rulFile.name}</p>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <File className="h-8 w-8 mx-auto text-muted-foreground" />
                        <p className="text-sm text-muted-foreground">Drop .rul file here or click to browse</p>
                      </div>
                    )}
                  </div>
                </div>

                <Button
                  type="submit"
                  disabled={isUploading || !dxfFile}
                  className="w-full h-12 text-base font-semibold shadow-warm hover:shadow-warm-lg transition-all duration-200"
                >
                  {isUploading ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Uploading...
                    </span>
                  ) : (
                    <span className="flex items-center gap-2">
                      <Upload className="h-4 w-4" />
                      Upload Pattern
                    </span>
                  )}
                </Button>
              </form>
            </CardContent>
          </Card>

          {/* Pattern List */}
          <Card className="border-border/50">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-secondary to-secondary/70 flex items-center justify-center">
                    <Package className="h-5 w-5 text-accent" />
                  </div>
                  <div>
                    <CardTitle className="text-lg">Pattern Library</CardTitle>
                    <CardDescription>
                      <span className="text-accent font-medium">{patterns.length}</span> patterns available
                    </CardDescription>
                  </div>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {isLoading ? (
                <div className="flex flex-col items-center justify-center py-12">
                  <Loader2 className="h-6 w-6 text-primary animate-spin" />
                  <p className="text-sm text-muted-foreground mt-2">Loading patterns...</p>
                </div>
              ) : patterns.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12">
                  <div className="w-14 h-14 rounded-2xl bg-muted flex items-center justify-center mb-3">
                    <Package className="h-7 w-7 text-muted-foreground" />
                  </div>
                  <p className="text-center text-muted-foreground">
                    No patterns uploaded yet
                  </p>
                </div>
              ) : (
                <div className="space-y-3 max-h-[400px] overflow-y-auto pr-2">
                  {patterns.map((pattern, index) => (
                    <div
                      key={pattern.id}
                      className="flex items-center justify-between p-4 bg-muted/50 rounded-xl hover:bg-muted transition-colors group"
                      style={{ animationDelay: `${index * 50}ms` }}
                    >
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-primary/10 to-accent/10 flex items-center justify-center group-hover:scale-105 transition-transform">
                          <Package className="h-5 w-5 text-primary" />
                        </div>
                        <div>
                          <p className="font-medium text-foreground">{pattern.name}</p>
                          <p className="text-xs text-muted-foreground">
                            {pattern.available_sizes.length > 0
                              ? <span>Sizes: <span className="text-accent">{pattern.available_sizes.join(', ')}</span></span>
                              : 'Not parsed'}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center">
                        {pattern.is_parsed ? (
                          <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-green-100/80 text-green-700 text-xs font-medium">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            Ready
                          </span>
                        ) : (
                          <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-muted text-muted-foreground text-xs font-medium">
                            <XCircle className="h-3.5 w-3.5" />
                            Pending
                          </span>
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
    </AuthGuard>
  )
}
