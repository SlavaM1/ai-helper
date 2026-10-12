import { useEffect, useRef, useState } from 'react'
import { SendIcon } from './Icons'

export function Composer({ disabled, streaming, onSend, onStop }: { disabled?: boolean; streaming?: boolean; onSend: (message: string) => void; onStop: () => void }) {
  const [value, setValue] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 180)}px`
  }, [value])

  const submit = () => {
    const message = value.trim()
    if (!message || disabled || streaming) return
    onSend(message)
    setValue('')
  }

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          placeholder={disabled ? 'Выберите или создайте диалог' : 'Спросите о коде, архитектуре или истории Git…'}
          aria-label="Сообщение"
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
        />
        {streaming ? (
          <button className="stop-button" type="button" onClick={onStop} aria-label="Остановить генерацию"><span /></button>
        ) : (
          <button className="send-button" type="button" onClick={submit} disabled={disabled || !value.trim()} aria-label="Отправить"><SendIcon /></button>
        )}
        <div className="composer__hint"><kbd>Enter</kbd> отправить <span><kbd>Shift</kbd> + <kbd>Enter</kbd> новая строка</span></div>
      </div>
    </div>
  )
}
