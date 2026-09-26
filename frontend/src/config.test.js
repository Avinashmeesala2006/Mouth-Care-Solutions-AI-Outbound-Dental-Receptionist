import { describe, expect, it } from 'vitest'
import { apiUrl } from './config'
describe('API configuration', () => {
  it('uses relative paths by default for same-origin deployments', () => expect(apiUrl('/health')).toBe('/health'))
  it('keeps endpoint paths stable', () => expect(apiUrl('/api/agent/session')).toContain('/api/agent/session'))
})
