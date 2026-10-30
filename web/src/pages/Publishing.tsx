import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button, Card, ConfirmDialog, Input, PageHeader, Select, Textarea } from '@/components'
import { useApiQuery, useSession } from '@/hooks'
import { apiDelete, apiPost, apiPut, isApiError } from '@/lib/api'
import { paths } from '@/lib/endpoints'
import { POLL } from '@/lib/query'
import { QrLoginDialog } from './identities/QrLoginDialog'
import styles from './Publishing.module.css'

interface Account { id: string; label: string; expiry_state: string }
interface Job {
  id: string; account_id: string; account_label: string; account_name: string; account_uid: string
  title: string; description: string; filename: string; size: number; state: string
  error_code: string | null; post_id: string | null; updated_at: string
}
interface Overview { configured: boolean; accounts: Account[]; items: Job[]; max_video_bytes: number }
const ACTIVE = new Set(['checking', 'uploading', 'submitting', 'receiving'])

export default function Publishing() {
  const { t } = useTranslation('console')
  const session = useSession()
  const [accountId, setAccountId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [scan, setScan] = useState(false)
  const [confirmation, setConfirmation] = useState<Job | null>(null)
  const [syncFailed, setSyncFailed] = useState<string[]>([])
  const inFlight = useRef(false)
  const synced = useRef(new Set<string>())
  const abort = useRef<AbortController | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const mounted = useRef(true)
  const query = useApiQuery<Overview>({
    key: ['publishing', session.data?.id], path: paths.publishing.root, poll: POLL.live,
    enabled: session.data?.role === 'admin', retry: false,
  })
  const data = query.data
  const jobs = data?.items ?? []
  const accounts = data?.accounts ?? []

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; abort.current?.abort() }
  }, [])
  useEffect(() => {
    for (const job of data?.items ?? []) {
      if (!['ready', 'published'].includes(job.state)) continue
      const key = `${session.data?.id}:${job.id}:${job.updated_at}`
      if (synced.current.has(key)) continue
      synced.current.add(key)
      void apiPost(paths.publishing.sync(job.id)).catch(() => {
        if (mounted.current) setSyncFailed((previous) => [...new Set([...previous, job.id])])
      })
    }
  }, [data, session.data?.id])

  function explain(cause: unknown): string {
    const reason = isApiError(cause) ? cause.details['reason'] : null
    return typeof reason === 'string'
      ? t(`publishing.errors.${reason}`, { defaultValue: t('publishing.errors.unavailable') })
      : t('publishing.errors.unavailable')
  }

  async function run(action: () => Promise<void>) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    try { await action() } catch (cause) { setError(explain(cause)) }
    finally {
      inFlight.current = false
      setBusy(false)
      await query.refetch()
    }
  }

  async function upload() {
    if (!file || !accountId || !title.trim() || !data) return
    if (file.size > data.max_video_bytes || file.size < 16 || !/\.(mp4|mov|m4v)$/i.test(file.name)) {
      setError(t('publishing.errors.invalid_video'))
      return
    }
    const controller = new AbortController()
    abort.current = controller
    const job = await apiPost<Job>(paths.publishing.jobs, {
      account_id: accountId, client_key: crypto.randomUUID().replaceAll('-', ''),
      title: title.trim(), description, filename: file.name, size: file.size,
    }, { signal: controller.signal })
    await apiPut(paths.publishing.video(job.id), file, { signal: controller.signal, timeoutMs: 900_000 })
    await apiPost(paths.publishing.prepare(job.id), undefined, { signal: controller.signal })
    setFile(null)
    if (fileInput.current) fileInput.current.value = ''
  }

  async function submit() {
    if (!confirmation) return
    const chosen = confirmation
    // Clear confirmation before awaiting: a lost response must not invite a second click.
    setConfirmation(null)
    await apiPost(paths.publishing.publish(chosen.id), { account_id: chosen.account_id, confirm: true })
  }

  const selected = accounts.find((account) => account.id === accountId)
  const workerBusy = jobs.some((job) => ACTIVE.has(job.state))
  return (
    <div className={styles.page}>
      <PageHeader title={t('publishing.title')} description={t('publishing.subtitle')} actions={
        <Button onClick={() => { setScan(true) }} disabled={busy || workerBusy}>{t('publishing.scan')}</Button>
      } />
      {session.data && session.data.role !== 'admin' ? <p role="alert">{t('publishing.adminOnly')}</p> : null}
      {query.isError ? <p role="alert" className={styles.notice}>{explain(query.error)}</p> : null}
      {data && !data.configured ? <p className={styles.notice}>{t('publishing.notConfigured')}</p> : null}
      {error ? <p role="alert" className={styles.notice}>{error}</p> : null}
      <Card title={t('publishing.newVideo')} description={t('publishing.prepareHint')}>
        <form className={styles.form} onSubmit={(event) => { event.preventDefault(); void run(upload) }}>
          <Select label={t('publishing.account')} value={accountId} required disabled={busy}
            onChange={(event) => { setAccountId(event.target.value) }} options={[
              { value: '', label: t('publishing.chooseAccount') },
              ...accounts.map((account) => ({ value: account.id, label: `${account.label}${account.expiry_state === 'expired' ? ` · ${t('publishing.expired')}` : ''}`, disabled: account.expiry_state === 'expired' })),
            ]} />
          {!accounts.length && data?.configured ? <p>{t('publishing.noAccounts')}</p> : null}
          <Input ref={fileInput} type="file" label={t('publishing.file')} description={t('publishing.fileHint')} accept=".mp4,.mov,.m4v" required disabled={busy}
            onChange={(event) => { setFile(event.target.files?.[0] ?? null) }} />
          <Input label={t('publishing.videoTitle')} value={title} required maxLength={30} disabled={busy}
            onChange={(event) => { setTitle(event.target.value) }} />
          <Textarea label={t('publishing.description')} value={description} maxLength={1000} rows={4} disabled={busy}
            onChange={(event) => { setDescription(event.target.value) }} />
          <div className={styles.actions}>
            <Button type="submit" variant="primary" loading={busy} disabled={busy || workerBusy || !file || !title.trim() || !selected || selected.expiry_state === 'expired' || !data?.configured}>
              {t('publishing.prepare')}
            </Button>
            {busy ? <span role="status">{t('publishing.working')}</span> : null}
          </div>
        </form>
      </Card>
      <Card title={t('publishing.history')} description={t('publishing.historyHint')}>
        {!jobs.length ? <p>{t('publishing.empty')}</p> : <div className={styles.jobs}>
          {jobs.map((job) => <article key={job.id} className={styles.job}>
            <div className={styles.jobHeading}><h3>{job.title}</h3><span role="status" className={styles.state}>{t(`publishing.states.${job.state}`)}</span></div>
            <p>{job.account_name || job.account_label}{job.account_uid ? ` · ${t('publishing.accountId')}: ${job.account_uid}` : ''}</p>
            <p className={styles.meta}>{job.filename} · {(job.size / 1024 / 1024).toFixed(1)} MB</p>
            {job.error_code ? <p className={styles.notice}>{t(`publishing.errors.${job.error_code}`, { defaultValue: t('publishing.errors.unavailable') })}</p> : null}
            {syncFailed.includes(job.id) ? <p role="alert">{t('publishing.syncFailed')}</p> : null}
            <div className={styles.actions}>
              {job.state === 'ready' ? <Button variant="primary" disabled={busy || workerBusy} onClick={() => { setConfirmation(job) }}>{t('publishing.publish')}</Button> : null}
              {['uploaded', 'ready', 'failed'].includes(job.state) ? <Button disabled={busy || workerBusy} onClick={() => { void run(async () => { await apiPost(paths.publishing.prepare(job.id)) }) }}>{t('publishing.recheck')}</Button> : null}
              {['ready', 'published'].includes(job.state) && syncFailed.includes(job.id) ? <Button disabled={busy} onClick={() => { void run(async () => { await apiPost(paths.publishing.sync(job.id)); setSyncFailed((ids) => ids.filter((id) => id !== job.id)) }) }}>{t('publishing.sync')}</Button> : null}
              {['awaiting_upload', 'uploaded', 'ready', 'failed'].includes(job.state) ? <Button variant="ghost" disabled={busy} onClick={() => { void run(async () => { await apiDelete(paths.publishing.job(job.id)) }) }}>{t('publishing.discard')}</Button> : null}
              {job.post_id ? <a href={`https://www.douyin.com/video/${job.post_id}`} target="_blank" rel="noreferrer">{t('publishing.openWork')}</a> : null}
              {job.state === 'uncertain' ? <a href="https://creator.douyin.com/creator-micro/content/manage" target="_blank" rel="noreferrer">{t('publishing.checkPlatform')}</a> : null}
            </div>
          </article>)}
        </div>}
      </Card>
      <ConfirmDialog open={!!confirmation} onCancel={() => { if (!busy) setConfirmation(null) }} onConfirm={() => { void run(submit) }} loading={busy}
        title={t('publishing.confirmTitle')} confirmLabel={t('publishing.publish')} description={t('publishing.confirmHint')}>
        {confirmation ? <div className={styles.confirm}><p><strong>{confirmation.account_name}</strong> · {confirmation.account_uid}</p><p>{confirmation.title}</p><p>{confirmation.description}</p></div> : null}
      </ConfirmDialog>
      {scan ? <QrLoginDialog onClose={() => { setScan(false) }} onDone={async () => { await query.refetch() }} onImported={() => { void query.refetch() }} /> : null}
    </div>
  )
}
