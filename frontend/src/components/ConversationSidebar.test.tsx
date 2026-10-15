import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ConversationSidebar } from './ConversationSidebar'

const conversations = [
  { id: '1', title: 'Разобрать авторизацию' },
  { id: '2', title: 'Проверить индексатор' },
]

function setup() {
  const callbacks = {
    onClose: vi.fn(),
    onViewChange: vi.fn(),
    onCreate: vi.fn(),
    onSelect: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  }
  render(<ConversationSidebar conversations={conversations} activeId="1" open loading={false} view="chat" {...callbacks} />)
  return callbacks
}

describe('ConversationSidebar', () => {
  it('открывает File Agent, сохраняя навигацию чата и reviews', async () => {
    const callbacks = setup()
    await userEvent.click(screen.getByRole('button', { name: 'File Agent' }))
    expect(callbacks.onViewChange).toHaveBeenCalledWith('file-agent')
    expect(callbacks.onClose).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Code Reviews' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Чат' })).toBeInTheDocument()
  })

  it('открывает раздел code reviews', async () => {
    const callbacks = setup()
    await userEvent.click(screen.getByRole('button', { name: 'Code Reviews' }))
    expect(callbacks.onViewChange).toHaveBeenCalledWith('reviews')
    expect(callbacks.onClose).toHaveBeenCalled()
  })

  it('запускает создание нового диалога', async () => {
    const callbacks = setup()
    await userEvent.click(screen.getByRole('button', { name: /новый диалог/i }))
    expect(callbacks.onCreate).toHaveBeenCalledOnce()
  })

  it('передаёт выбранный диалог на подтверждение удаления', async () => {
    const callbacks = setup()
    await userEvent.click(screen.getByRole('button', { name: 'Удалить «Разобрать авторизацию»' }))
    expect(callbacks.onDelete).toHaveBeenCalledWith(conversations[0])
  })

  it('переименовывает диалог с клавиатуры', async () => {
    const callbacks = setup()
    await userEvent.click(screen.getByRole('button', { name: 'Переименовать «Разобрать авторизацию»' }))
    const input = screen.getByRole('textbox', { name: 'Название диалога' })
    await userEvent.clear(input)
    await userEvent.type(input, 'Новый заголовок{Enter}')
    expect(callbacks.onRename).toHaveBeenCalledWith('1', 'Новый заголовок')
  })
})
