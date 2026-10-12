import { describe, expect, it } from 'vitest'
import { closeLine, inputFrame, resizeFrame } from './workbench'

describe('terminal frames', () => {
  it('encodes input and resize as the server parses them', () => {
    expect(JSON.parse(inputFrame('ls "a"\r'))).toEqual({ type: 'input', data: 'ls "a"\r' })
    expect(JSON.parse(resizeFrame(120, 40))).toEqual({ type: 'resize', cols: 120, rows: 40 })
  })
})

describe('closeLine', () => {
  it('says the shell exited on a normal close', () => {
    expect(closeLine(1000, '')).toBe('Disconnected: the shell exited.')
  })

  it('gives the server reason, else the code', () => {
    expect(closeLine(1011, 'shell did not start')).toBe('Disconnected: shell did not start.')
    expect(closeLine(1009, 'too much input pending')).toBe('Disconnected: too much input pending.')
    expect(closeLine(1006, '')).toBe('Disconnected: code 1006.')
  })
})
