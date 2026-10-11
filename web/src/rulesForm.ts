import { ApiError } from './api'

/** A path into the rules object; the same shape as a pydantic error's `loc`. */
export type Path = readonly (string | number)[]
export type FieldError = { loc: Path; msg: string }

type Node = Record<string | number, unknown>

/** A copy of `obj` with `value` at `path`. Nothing is mutated; untouched branches are shared, so fields the form does not show round-trip as they came. */
export function setIn<T>(obj: T, path: Path, value: unknown): T {
  const [head, ...rest] = path
  if (head === undefined) return value as T
  if (Array.isArray(obj)) return obj.map((v, i) => (i === head ? setIn(v, rest, value) : v)) as T
  const node = (obj ?? {}) as Node
  return { ...node, [head]: setIn(node[head], rest, value) } as T
}

export const getIn = (obj: unknown, path: Path): unknown =>
  path.reduce<unknown>((o, k) => (o == null ? undefined : (o as Node)[k]), obj)

const same = (a: Path, b: Path) => a.length === b.length && a.every((k, i) => k === b[i])

/** Messages of the errors whose `loc` is exactly `path`. */
export const errorsAt = (errors: readonly FieldError[], path: Path): string[] =>
  errors.filter((e) => same(e.loc, path)).map((e) => e.msg)

/** The `errors` list of a 422 save (contracts: Plan 15), or [] for any other error. */
export function fieldErrors(e: unknown): FieldError[] {
  if (!(e instanceof ApiError) || e.status !== 422) return []
  try {
    const errors: unknown = JSON.parse(e.body).errors
    return Array.isArray(errors) ? errors.filter((x) => Array.isArray(x?.loc) && typeof x?.msg === 'string') : []
  } catch {
    return []
  }
}
