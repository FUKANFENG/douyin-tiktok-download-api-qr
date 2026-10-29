import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button, Input, Modal, useToast } from '@/components'
import { useApiMutation, useApiQuery, useSession } from '@/hooks'
import { apiDelete, apiPost, isApiError } from '@/lib/api'
import { paths } from '@/lib/endpoints'
import { POLL } from '@/lib/query'

import styles from './qrLogin.module.css'

interface SavedAccount {
  id: string
  label: string
  cookie_count: number
  expires_at: string | null
  expiry_state: 'unknown' | 'expired' | 'expiring' | 'valid'
}

interface LoginSession {
  id: string
  status: 'starting' | 'waiting' | 'scanned' | 'verification_required' | 'complete' | 'cancelled' | 'expired' | 'error'
  expires_at: string
  qr_available: boolean
  image_revision: number
  account: SavedAccount | null
}

interface Overview {
  configured: boolean
  accounts: SavedAccount[]
  session: LoginSession | null
}

const TERMINAL = new Set(['complete', 'cancelled', 'expired', 'error'])

function readPending(key: string): string | null {
  try { return sessionStorage.getItem(key) } catch { return null }
}

export function QrLoginDialog({ onClose, onDone, onImported }: {
  onClose: () => void
  onDone: () => Promise<void>
  onImported: (id: string) => void
}) {
  const { t, i18n } = useTranslation(['console', 'common'])
  const toast = useToast()
  const currentUser = useSession()
  // A session ID is not a credential. Keep it only in this tab, scoped to its operator.
  const pendingKey = currentUser.data ? `dtk.qr-login.pending:${currentUser.data.id}` : null
  const [label, setLabel] = useState('')
  const [code, setCode] = useState('')
  const [imageFailed, setImageFailed] = useState(false)
  const watchedSession = useRef<string | null>(null)
  const attemptedImport = useRef<string | null>(null)

  const remember = useCallback((id: string | null) => {
    watchedSession.current = id
    if (!pendingKey) return
    try {
      if (id) sessionStorage.setItem(pendingKey, id)
      else sessionStorage.removeItem(pendingKey)
    } catch { /* The active scan still works when browser storage is unavailable. */ }
  }, [pendingKey])

  const overview = useApiQuery<Overview>({
    key: ['qr-login', currentUser.data?.id], path: paths.qrLogin.root, retry: false, staleTime: 0,
    enabled: !!pendingKey,
    refetchInterval: (query) => {
      const session = query.state.data?.session
      return session && !TERMINAL.has(session.status) ? POLL.live : false
    },
  })
  const session = overview.data?.session
  const active = !!session && !TERMINAL.has(session.status)

  function explain(error: unknown): string {
    const reason = isApiError(error) ? error.details['reason'] : null
    return typeof reason === 'string'
      ? t(`console:qrLogin.errors.${reason}`, { defaultValue: t('console:qrLogin.unavailable') })
      : t('console:qrLogin.unavailable')
  }

  const start = useApiMutation<LoginSession, string | null>(
    (replaceId) => apiPost(paths.qrLogin.sessions, { label: label.trim(), replace_id: replaceId }),
    {
      onSuccess: async (created) => {
        remember(created.id)
        attemptedImport.current = null
        cancel.reset()
        submitCode.reset()
        imported.reset()
        setCode('')
        setImageFailed(false)
        await overview.refetch()
      },
    },
  )
  const cancel = useApiMutation<LoginSession, string>(
    (id) => apiDelete<LoginSession>(paths.qrLogin.session(id)),
    {
      onSuccess: async (cancelled) => {
        if (cancelled.status === 'cancelled') remember(null)
        start.reset()
        submitCode.reset()
        imported.reset()
        await overview.refetch()
      },
    },
  )
  const submitCode = useApiMutation<{ submitted: boolean }, { id: string; code: string }>(
    (submission) => apiPost(paths.qrLogin.code(submission.id), { code: submission.code }),
    { onSuccess: () => { setCode(''); toast.success(t('console:qrLogin.codeSubmitted')) } },
  )
  const imported = useApiMutation<{ stored: boolean; identity_id: string }, string>(
    (id) => apiPost(paths.qrLogin.import(id)),
    {
      onSuccess: async (result, accountId) => {
        if (session?.account?.id === accountId) remember(null)
        toast.success(t(result.stored ? 'console:qrLogin.imported' : 'console:qrLogin.alreadyImported'))
        await onDone()
        onImported(result.identity_id)
      },
    },
  )
  const importAccount = imported.mutate

  useEffect(() => {
    if (!session || !pendingKey) return
    if (!TERMINAL.has(session.status)) remember(session.id)
    const pending = watchedSession.current ?? readPending(pendingKey)
    if (
      session.status === 'complete' && session.account && pending === session.id
      && attemptedImport.current !== session.id
    ) {
      attemptedImport.current = session.id
      importAccount(session.account.id)
    } else if (session.status !== 'complete' && TERMINAL.has(session.status) && pending === session.id) {
      remember(null)
    }
  }, [session, pendingKey, remember, importAccount])

  const busy = start.isPending || cancel.isPending || imported.isPending || submitCode.isPending
  const error = overview.error ?? start.error ?? cancel.error ?? submitCode.error ?? imported.error
  const close = () => { if (!busy) onClose() }

  function retryFailedAction() {
    if (busy) return
    if (overview.error) { void overview.refetch(); return }
    if (start.error) {
      if (start.error.details['reason'] === 'qr_busy') {
        start.reset()
        void overview.refetch()
      } else start.mutate(start.variables ?? null)
      return
    }
    if (cancel.error && cancel.variables) { cancel.mutate(cancel.variables); return }
    if (submitCode.error && submitCode.variables) { submitCode.mutate(submitCode.variables); return }
    if (imported.error && imported.variables) imported.mutate(imported.variables)
  }

  return (
    <Modal
      open onClose={close} title={t('console:qrLogin.title')}
      description={t('console:qrLogin.description')} closeOnBackdrop={!busy}
      footer={
        <>
          {active ? <Button onClick={() => { if (session) cancel.mutate(session.id) }} disabled={busy}>{t('console:qrLogin.cancel')}</Button> : null}
          <Button variant="ghost" onClick={close} disabled={busy}>{t('common:action.close')}</Button>
        </>
      }
    >
      <div className="u-stack">
        {error ? (
          <div role="alert" className={styles.notice}>
            <p>{explain(error)}</p>
            <Button size="sm" onClick={retryFailedAction} disabled={busy}>{t('common:action.retry')}</Button>
          </div>
        ) : null}
        {overview.isPending ? <p role="status">{t('console:qrLogin.loading')}</p> : null}
        {overview.data && !overview.data.configured ? <p>{t('console:qrLogin.notConfigured')}</p> : null}
        {overview.data?.configured ? (
          <>
            <Input label={t('console:qrLogin.label')} maxLength={80} value={label} disabled={busy || active}
              onChange={(event) => { setLabel(event.target.value) }} />
            {!active ? (
              <Button variant="primary" loading={start.isPending} disabled={busy} onClick={() => { start.mutate(null) }}>
                {t('console:qrLogin.start')}
              </Button>
            ) : null}
            {session ? (
              <div className={styles.session}>
                <p role="status" aria-live="polite">{t(`console:qrLogin.status.${session.status}`)}</p>
                {active ? <p className="u-muted">{t('console:qrLogin.expires', { time: new Date(session.expires_at).toLocaleTimeString(i18n.language) })}</p> : null}
                {session.qr_available && active && !overview.error ? (
                  <img className={styles.qr} width={240} height={240}
                    src={`${paths.qrLogin.qr(session.id)}?v=${session.image_revision}`}
                    alt={t('console:qrLogin.qrAlt')} onError={() => { setImageFailed(true) }}
                    onLoad={() => { setImageFailed(false) }} />
                ) : null}
                {imageFailed && active ? <p role="alert">{t('console:qrLogin.imageFailed')}</p> : null}
                {session.status === 'verification_required' ? (
                  <form className="u-stack-sm" onSubmit={(event) => { event.preventDefault(); if (/^[0-9]{4,8}$/.test(code) && !busy) submitCode.mutate({ id: session.id, code }) }}>
                    <Input label={t('console:qrLogin.code')} value={code} inputMode="numeric" autoComplete="one-time-code"
                      minLength={4} maxLength={8} pattern="[0-9]{4,8}" required disabled={busy}
                      onChange={(event) => { setCode(event.target.value.replace(/\D/g, '')) }} />
                    <Button type="submit" disabled={busy || !/^[0-9]{4,8}$/.test(code)}>{t('console:qrLogin.submitCode')}</Button>
                  </form>
                ) : null}
                {session.status === 'complete' && session.account ? (
                  <Button loading={imported.isPending} disabled={busy} onClick={() => { if (session.account) imported.mutate(session.account.id) }}>
                    {t('console:qrLogin.import')}
                  </Button>
                ) : null}
              </div>
            ) : null}
            <section className="u-stack-sm" aria-label={t('console:qrLogin.saved')}>
              <h3>{t('console:qrLogin.saved')}</h3>
              {overview.data.accounts.length === 0 ? <p className="u-muted">{t('console:qrLogin.empty')}</p> : null}
              {overview.data.accounts.map((account) => (
                <div key={account.id} className={styles.account}>
                  <div className={styles.accountText}>
                    <strong>{account.label}</strong>
                    <p className="u-muted">{t('console:qrLogin.cookies', { count: account.cookie_count })}</p>
                    <p className="u-muted">{account.expires_at
                      ? t('console:qrLogin.accountExpiry', { time: new Date(account.expires_at).toLocaleString(i18n.language) })
                      : t('console:qrLogin.unknownExpiry')}</p>
                    {account.expiry_state === 'expired' || account.expiry_state === 'expiring'
                      ? <p>{t(`console:qrLogin.${account.expiry_state}`)}</p> : null}
                  </div>
                  <div className={styles.actions}>
                    <Button size="sm" disabled={busy || active || account.expiry_state === 'expired'} onClick={() => { imported.mutate(account.id) }}>{t('console:qrLogin.import')}</Button>
                    <Button size="sm" variant="ghost" disabled={busy || active} onClick={() => { start.mutate(account.id) }}>{t('console:qrLogin.renew')}</Button>
                  </div>
                </div>
              ))}
            </section>
            <p className="u-muted">{t('console:qrLogin.note')}</p>
          </>
        ) : null}
      </div>
    </Modal>
  )
}
