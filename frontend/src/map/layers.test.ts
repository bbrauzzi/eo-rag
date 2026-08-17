import { describe, expect, it } from 'vitest'

import type { StacFeature } from '../api/types'
import { imageCorners } from './layers'

/**
 * Corner ordering is worth testing on its own: getting it wrong does not fail, it
 * silently mirrors or rotates the quicklook over an otherwise correct footprint.
 */

const footprint = (geometry: unknown, bbox: number[] | null = null) =>
  ({
    type: 'Feature',
    id: 'S2B_test',
    bbox,
    geometry,
    properties: { kind: 'footprint', id: 'S2B_test' },
  }) as unknown as StacFeature

describe('imageCorners', () => {
  it('orders a rotated MGRS tile north-west, north-east, south-east, south-west', () => {
    // The real footprint of S2B_33TTG, which is a few degrees off north - the case the
    // bbox would stretch and this exists to get right.
    const ring = [
      [11.354967, 42.394693],
      [11.410743, 41.407736],
      [12.723021, 41.441221],
      [12.687581, 42.429352],
      [11.354967, 42.394693],
    ]

    const corners = imageCorners(footprint({ type: 'Polygon', coordinates: [ring] }))!

    expect(corners).toEqual([
      [11.354967, 42.394693], // north-west
      [12.687581, 42.429352], // north-east
      [12.723021, 41.441221], // south-east
      [11.410743, 41.407736], // south-west
    ])
  })

  it('is insensitive to where the ring starts', () => {
    const ring = [
      [0, 1],
      [1, 1],
      [1, 0],
      [0, 0],
    ]
    const rotated = [...ring.slice(2), ...ring.slice(0, 2)]

    const a = imageCorners(footprint({ type: 'Polygon', coordinates: [[...ring, ring[0]]] }))
    const b = imageCorners(footprint({ type: 'Polygon', coordinates: [[...rotated, rotated[0]]] }))

    expect(a).toEqual(b)
    expect(a).toEqual([
      [0, 1],
      [1, 1],
      [1, 0],
      [0, 0],
    ])
  })

  it('falls back to the bbox for a geometry that is not a four-corner ring', () => {
    // What the antimeridian split produces, and what a swath outline looks like.
    const multi = { type: 'MultiPolygon', coordinates: [[[[170, -10], [180, -10], [180, 10], [170, 10], [170, -10]]]] }

    const corners = imageCorners(footprint(multi, [170, -10, 180, 10]))

    expect(corners).toEqual([
      [170, 10],
      [180, 10],
      [180, -10],
      [170, -10],
    ])
  })

  it('gives up rather than guessing when there is neither a usable ring nor a bbox', () => {
    expect(imageCorners(footprint({ type: 'Point', coordinates: [12, 42] }))).toBeNull()
  })
})
