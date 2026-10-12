import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { StatsDrawer } from './StatsDrawer'

describe('StatsDrawer', () => {
  it('показывает usage текущего диалога, агрегат и сводку', () => {
    render(
      <StatsDrawer
        open
        onClose={vi.fn()}
        tools={[{ name: 'git_log', description: 'История коммитов' }]}
        current={{ promptTokens: 120, completionTokens: 80, totalTokens: 200, requests: 4, cost: 0.01 }}
        aggregate={{ promptTokens: 900, completionTokens: 600, totalTokens: 1500, requests: 12, cost: 0.08 }}
        summary={{ content: 'Обсуждалась маршрутизация запросов.', messageCount: 10 }}
      />,
    )

    expect(screen.getByText('200')).toBeInTheDocument()
    expect(screen.getByText(/1.500/)).toBeInTheDocument()
    expect(screen.getByText('Обсуждалась маршрутизация запросов.')).toBeInTheDocument()
    expect(screen.getByText('git_log')).toBeInTheDocument()
  })
})
