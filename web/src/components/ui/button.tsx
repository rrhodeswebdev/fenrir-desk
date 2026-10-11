import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

// DESIGN.md "Control": outlined, square, tracked mono caps. Colour lives only in the
// right edge block (inset shadow): red = destructive, ember = caution, ember on hover/focus
// for a plain control. No filled variant.
const buttonVariants = cva(
  "step inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 border border-input bg-transparent font-mono text-[11px] leading-none font-medium tracking-label whitespace-nowrap text-ink uppercase hover:border-ink focus-visible:border-ink disabled:cursor-not-allowed disabled:border-rule-strong disabled:text-ink-3 aria-invalid:border-red [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        outline:
          "hover:shadow-[inset_-10px_0_var(--ember)] focus-visible:shadow-[inset_-10px_0_var(--ember)] disabled:shadow-none",
        destructive: "shadow-[inset_-15px_0_var(--red)]",
        // 14px Ember block behind a 1px rule that steps to Ice on hover.
        caution:
          "shadow-[inset_-14px_0_var(--ember),inset_-15px_0_var(--ink-3)] hover:shadow-[inset_-14px_0_var(--ember),inset_-15px_0_var(--ink)]",
      },
      size: {
        default: "h-11 px-6",
        sm: "h-8 px-4",
        icon: "size-11",
      },
    },
    compoundVariants: [
      // Keep the caption's padding clear of the 15px edge block.
      { variant: ["destructive", "caution"], size: "default", class: "pr-[39px]" },
      { variant: ["destructive", "caution"], size: "sm", class: "pr-[31px]" },
    ],
    defaultVariants: {
      variant: "outline",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "outline",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
