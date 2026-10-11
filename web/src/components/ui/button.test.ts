import { describe, expect, it } from 'vitest'
import { buttonVariants } from './button'

describe('buttonVariants', () => {
  it('defaults to the plain outlined control', () => {
    expect(buttonVariants()).toBe(buttonVariants({ variant: 'outline', size: 'default' }))
    expect(buttonVariants()).toContain('border-input')
    expect(buttonVariants()).toContain('hover:shadow-[inset_-10px_0_var(--ember)]')
  })

  it('draws a red edge for destructive and an ember edge for caution, clear of the caption', () => {
    expect(buttonVariants({ variant: 'destructive' })).toContain('shadow-[inset_-15px_0_var(--red)]')
    expect(buttonVariants({ variant: 'caution' })).toContain('shadow-[inset_-14px_0_var(--ember)')
    expect(buttonVariants({ variant: 'caution', size: 'sm' })).toContain('pr-[31px]')
  })

  it('has no filled variant', () => {
    // @ts-expect-error shadcn's filled `default` variant is removed (DESIGN.md: no filled buttons)
    expect(buttonVariants({ variant: 'default' })).not.toMatch(/\bbg-(?!transparent)/)
  })
})
