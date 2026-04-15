'use client'

import { useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { useAuthStore } from '@/lib/auth-store'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import {
  LayoutDashboard,
  FileText,
  Package,
  Settings,
  LogOut,
  Menu,
  X,
  ChevronDown,
  Scissors,
  Cpu,
} from 'lucide-react'

const navigation = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Orders', href: '/orders', icon: FileText },
  { name: 'Patterns', href: '/patterns', icon: Package },
  { name: 'Queue', href: '/queue', icon: Cpu },
  { name: 'Fabrics', href: '/settings/fabrics', icon: Scissors },
  { name: 'Settings', href: '/settings', icon: Settings },
]

export function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const router = useRouter()
  const { user, logout } = useAuthStore()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const handleLogout = async () => {
    await logout()
    router.push('/login')
  }

  return (
    <div className="min-h-screen bg-background bg-pattern">
      {/* Mobile sidebar */}
      <div
        className={cn(
          'fixed inset-0 z-50 lg:hidden transition-opacity duration-300',
          sidebarOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
        )}
      >
        <div
          className="fixed inset-0 bg-foreground/40 backdrop-blur-sm"
          onClick={() => setSidebarOpen(false)}
        />
        <div className={cn(
          "fixed inset-y-0 left-0 flex w-72 flex-col bg-card shadow-warm-lg transition-transform duration-300 ease-out",
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        )}>
          <div className="flex h-16 items-center justify-between px-4 border-b border-border">
            <div className="flex items-center">
              <div className="w-10 h-10 bg-gradient-to-br from-primary to-accent rounded-xl flex items-center justify-center shadow-warm">
                <span className="text-xl font-bold text-primary-foreground">M</span>
              </div>
              <span className="ml-3 text-xl font-bold text-foreground">MarkerMind</span>
            </div>
            <button
              onClick={() => setSidebarOpen(false)}
              className="p-2 rounded-lg hover:bg-muted transition-colors"
            >
              <X className="h-5 w-5 text-muted-foreground" />
            </button>
          </div>
          <nav className="flex-1 space-y-1 px-3 py-4">
            {navigation.map((item) => (
              <Link
                key={item.name}
                href={item.href}
                className={cn(
                  'flex items-center px-4 py-3 text-sm font-medium rounded-xl transition-all duration-200',
                  pathname === item.href
                    ? 'bg-primary text-primary-foreground shadow-warm'
                    : 'text-foreground hover:bg-muted hover:translate-x-1'
                )}
                onClick={() => setSidebarOpen(false)}
              >
                <item.icon className="mr-3 h-5 w-5" />
                {item.name}
              </Link>
            ))}
          </nav>
        </div>
      </div>

      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-72 lg:flex-col">
        <div className="flex min-h-0 flex-1 flex-col border-r border-border bg-card/80 backdrop-blur-sm">
          <div className="flex h-16 items-center px-4 border-b border-border">
            <div className="w-10 h-10 bg-gradient-to-br from-primary to-accent rounded-xl flex items-center justify-center shadow-warm">
              <span className="text-xl font-bold text-primary-foreground">M</span>
            </div>
            <span className="ml-3 text-xl font-bold text-foreground">MarkerMind</span>
          </div>
          <nav className="flex-1 space-y-1 px-3 py-4">
            {navigation.map((item) => (
              <Link
                key={item.name}
                href={item.href}
                className={cn(
                  'flex items-center px-4 py-3 text-sm font-medium rounded-xl transition-all duration-200',
                  pathname === item.href
                    ? 'bg-primary text-primary-foreground shadow-warm'
                    : 'text-foreground hover:bg-muted hover:translate-x-1'
                )}
              >
                <item.icon className="mr-3 h-5 w-5" />
                {item.name}
              </Link>
            ))}
          </nav>
          <div className="border-t border-border p-4">
            <div className="flex items-center rounded-xl bg-muted/50 p-3">
              <div className="flex-shrink-0">
                <div className="w-10 h-10 bg-gradient-to-br from-secondary to-accent/30 rounded-full flex items-center justify-center ring-2 ring-border">
                  <span className="text-sm font-semibold text-accent">
                    {user?.name?.charAt(0).toUpperCase()}
                  </span>
                </div>
              </div>
              <div className="ml-3 flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">{user?.name}</p>
                <p className="text-xs text-muted-foreground truncate">{user?.email}</p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={handleLogout}
                title="Logout"
                className="hover:bg-destructive/10 hover:text-destructive"
              >
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Main content */}
      <div className="lg:pl-72">
        {/* Top bar */}
        <div className="sticky top-0 z-40 flex h-16 shrink-0 items-center gap-x-4 border-b border-border bg-card/80 backdrop-blur-md px-4 shadow-warm lg:px-8">
          <button
            type="button"
            className="lg:hidden p-2 rounded-lg hover:bg-muted transition-colors"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-5 w-5 text-foreground" />
          </button>
          <div className="flex flex-1 gap-x-4 self-stretch lg:gap-x-6">
            <div className="flex flex-1" />
            <div className="flex items-center gap-x-4 lg:gap-x-6">
              <span className="text-sm text-muted-foreground">
                Welcome back, <span className="font-medium text-foreground">{user?.name}</span>
              </span>
            </div>
          </div>
        </div>

        {/* Page content */}
        <main className="py-8 px-4 lg:px-8">{children}</main>
      </div>
    </div>
  )
}
