import * as React from "react"
import { cn } from "@/lib/utils"

// DESIGN.md: square, 1px Slate outline stepping to Ice on hover and focus, Value type; Red outline when invalid.
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "step value h-9 w-full min-w-0 border border-input bg-transparent px-3 placeholder:text-ink-3 hover:border-ink focus-visible:border-ink disabled:cursor-not-allowed disabled:border-rule-strong disabled:text-ink-3 aria-invalid:border-red",
        className
      )}
      {...props}
    />
  )
}

export { Input }
