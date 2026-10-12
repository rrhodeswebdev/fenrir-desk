import * as React from "react"
import { cn } from "@/lib/utils"
import { Switch as SwitchPrimitive } from "radix-ui"

// DESIGN.md: an outlined square track; the thumb is a Slate block at the left when off, an Ice block at the right when on.
function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        "peer step inline-flex h-5 w-9 shrink-0 cursor-pointer items-center border border-input px-[3px] hover:border-ink focus-visible:border-ink disabled:cursor-not-allowed disabled:border-rule-strong aria-invalid:border-red",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className="pointer-events-none block size-3 bg-ink-3 data-[state=checked]:translate-x-[16px] data-[state=checked]:bg-ink"
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
