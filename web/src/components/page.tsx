import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/** Top strip (page title, then optional strip content such as the kill switch) above the page body. `className` overrides body padding. */
export function Page({
  title,
  strip,
  className,
  children,
}: {
  title: string
  strip?: ReactNode
  className?: string
  children?: ReactNode
}) {
  return (
    <>
      <header className="flex min-h-[72px] items-center gap-6 border-b px-8 whitespace-nowrap max-md:flex-wrap max-md:gap-4 max-md:p-4 max-md:whitespace-normal">
        <h1>{title}</h1>
        {strip}
      </header>
      <div className={cn('p-8 max-md:px-4', className)}>{children}</div>
    </>
  )
}
