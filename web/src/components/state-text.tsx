import type { ReactNode } from 'react'

export type Tone = 'normal' | 'idle' | 'caution' | 'alarm'

// DESIGN.md Quarantine Rule: Steel normal, Slate idle (Unlit is too dark for text), Ember caution, Red alarm.
export const toneClass: Record<Tone, string> = {
  normal: 'text-ink-2',
  idle: 'text-ink-3',
  caution: 'text-ember',
  alarm: 'text-red',
}

/** A one-word status in Label type, coloured by tone. Replaces Badge. */
export function StateText({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`font-mono text-[11px] leading-[1.4] font-medium tracking-label uppercase ${toneClass[tone]}`}>
      {children}
    </span>
  )
}
