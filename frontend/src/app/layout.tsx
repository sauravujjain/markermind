import type { Metadata } from 'next'
import './globals.css'
import { Providers } from '@/components/providers'

export const metadata: Metadata = {
  title: 'MarkerMind - Cutting Optimization Platform',
  description: 'AI-powered cutting optimization for garment manufacturing',
}

/**
 * Inline critical CSS so the login page is styled even when JS/CSS bundles
 * fail to load (e.g. stale production build after code push, slow connection).
 *
 * Hydration-check script: if React hasn't hydrated within 6 seconds, force
 * a hard reload (cache-bust) to pick up the fresh build.  Only fires once
 * per page load to avoid reload loops.
 */
const CRITICAL_CSS = `
  body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #F7F4F0; color: #373330; }
  input[type="email"], input[type="password"], input[type="text"] {
    display: block; width: 100%; box-sizing: border-box; padding: 0.625rem 0.75rem;
    border: 1px solid #d5cfc5; border-radius: 0.5rem; font-size: 0.875rem;
    background: #faf8f5; margin-top: 0.25rem;
  }
  input:focus { outline: 2px solid #BE5A38; outline-offset: -1px; border-color: #BE5A38; }
  button[type="submit"] {
    display: block; width: 100%; padding: 0.75rem; border: none; border-radius: 0.5rem;
    background: #BE5A38; color: #FDFCFA; font-weight: 600; font-size: 0.95rem; cursor: pointer;
    margin-top: 1rem;
  }
  button[type="submit"]:hover { background: #a44e30; }
  label { display: block; font-size: 0.8rem; font-weight: 500; color: #6b6560; }
`

const HYDRATION_CHECK_SCRIPT = `
(function() {
  var t = setTimeout(function() {
    if (!document.querySelector('[data-reactroot]') && !document.getElementById('__next')?.children.length) {
      // React hasn't hydrated — likely stale JS chunks. Hard reload once.
      if (!sessionStorage.getItem('_mm_reload')) {
        sessionStorage.setItem('_mm_reload', '1');
        window.location.reload();
      }
    }
  }, 6000);
  // Clear the flag on successful load so future stale builds also get caught
  window.addEventListener('load', function() {
    clearTimeout(t);
    sessionStorage.removeItem('_mm_reload');
  });
})();
`

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <style dangerouslySetInnerHTML={{ __html: CRITICAL_CSS }} />
        <script dangerouslySetInnerHTML={{ __html: HYDRATION_CHECK_SCRIPT }} />
      </head>
      <body className="font-sans">
        <Providers>
          {children}
        </Providers>
      </body>
    </html>
  )
}
