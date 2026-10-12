import * as React from "react"
import { cn } from "@/lib/utils"
import { Tabs as TabsPrimitive } from "radix-ui"

// DESIGN.md: tabs read like the index rail. Label-type triggers in Slate, Ice on hover; the active
// one is the rail's boxed cell (Night Deep ground, 1px Slate inset outline, Ember block at the right).
// No fill, radius or ring: the base Ember focus outline shows.

function Tabs({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      className={cn("flex flex-col gap-3", className)}
      {...props}
    />
  )
}

function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn("inline-flex w-fit items-center", className)}
      {...props}
    />
  )
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "step flex h-11 cursor-pointer items-center gap-3 px-6 font-mono text-[11px] leading-none font-medium tracking-label whitespace-nowrap text-ink-3 uppercase hover:text-ink disabled:cursor-not-allowed data-[state=active]:bg-ground-deep data-[state=active]:text-ink data-[state=active]:shadow-[inset_0_0_0_1px_var(--ink-3)] data-[state=active]:after:h-4 data-[state=active]:after:w-2 data-[state=active]:after:bg-ember",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent }
