import { useEffect, useRef } from 'react'
import type { Conversation } from '../types'

export function ConfirmDialog({ conversation, busy, onCancel, onConfirm }: { conversation?: Conversation; busy?: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (conversation && !dialog?.open) dialog?.showModal()
    if (!conversation && dialog?.open) dialog.close()
  }, [conversation])

  return (
    <dialog ref={dialogRef} className="confirm-dialog" onCancel={(event) => { event.preventDefault(); onCancel() }} onClose={onCancel}>
      <h2>Удалить диалог?</h2>
      <p>«{conversation?.title}» и все сообщения будут удалены без возможности восстановления.</p>
      <div>
        <button type="button" className="button-secondary" onClick={onCancel} disabled={busy}>Отмена</button>
        <button type="button" className="button-danger" onClick={onConfirm} disabled={busy}>{busy ? 'Удаление…' : 'Удалить'}</button>
      </div>
    </dialog>
  )
}
