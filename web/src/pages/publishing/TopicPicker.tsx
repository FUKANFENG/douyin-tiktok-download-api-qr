import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button, Input } from '@/components'
import { apiPost, isApiError } from '@/lib/api'
import { paths } from '@/lib/endpoints'
import styles from '../Publishing.module.css'

export interface Topic { id: string | null; name: string; source: 'search' | 'recommend' | 'new' }
export interface TopicCandidate extends Topic { views: number | null }

interface Props {
  accountId: string
  value: Topic[]
  onChange: (topics: Topic[]) => void
  disabled?: boolean
  suggestions?: TopicCandidate[]
  recommendationsLoaded?: boolean
  onSearchBusy?: (busy: boolean) => void
}

export default function TopicPicker({ accountId, value, onChange, disabled = false, suggestions, recommendationsLoaded, onSearchBusy }: Props) {
  const { t, i18n } = useTranslation('console')
  const [keyword, setKeyword] = useState('')
  const [results, setResults] = useState<TopicCandidate[]>([])
  const [searched, setSearched] = useState(false)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const searchLock = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; request.current?.abort(); if (searchLock.current) onSearchBusy?.(false) }
  }, [onSearchBusy])

  async function search() {
    const text = keyword.trim().replace(/^#/, '')
    if (!accountId || !text || searchLock.current || disabled) return
    searchLock.current = true
    onSearchBusy?.(true)
    const controller = new AbortController()
    request.current = controller
    setSearching(true)
    setError('')
    setResults([])
    setSearched(false)
    try {
      const result = await apiPost<{ items: TopicCandidate[] }>(paths.publishing.topicSearch,
        { account_id: accountId, keyword: text }, { signal: controller.signal, timeoutMs: 100_000 })
      if (!controller.signal.aborted && mounted.current) { setResults(result.items); setSearched(true) }
    } catch (cause) {
      if (!controller.signal.aborted && mounted.current) {
        const reason = isApiError(cause) ? cause.details['reason'] : null
        setError(typeof reason === 'string' ? t(`publishing.errors.${reason}`, { defaultValue: t('publishing.errors.topic_search_failed') }) : t('publishing.errors.topic_search_failed'))
      }
    } finally {
      searchLock.current = false
      onSearchBusy?.(false)
      if (mounted.current) setSearching(false)
    }
  }

  function selected(topic: Topic) {
    return value.some((item) => item.name.toLowerCase() === topic.name.toLowerCase() || (!!topic.id && item.id === topic.id))
  }
  function add(topic: Topic) {
    if (disabled || searching || value.length >= 5 || selected(topic)) return
    onChange([...value, { id: topic.id, name: topic.name, source: topic.source }])
  }
  function candidate(topic: TopicCandidate) {
    return <Button key={`${topic.source}:${topic.id}`} type="button" variant="ghost" disabled={disabled || searching || value.length >= 5 || selected(topic)} onClick={() => { add(topic) }}>
      #{topic.name}{topic.views !== null ? ` (${t('publishing.topics.views', { count: new Intl.NumberFormat(i18n.language, { notation: 'compact', maximumFractionDigits: 1 }).format(topic.views) })})` : ''}
    </Button>
  }
  const custom = keyword.trim().replace(/^#/, '')
  const validCustom = /^[^\s#@]{1,100}$/.test(custom) && ![...custom].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  return <fieldset className={styles.topics} disabled={disabled}>
    <legend>{t('publishing.topics.title', { count: value.length })}</legend>
    <p className={styles.meta}>{t('publishing.topics.hint')}</p>
    <div className={styles.topicSearch}>
      <Input label={t('publishing.topics.keyword')} value={keyword} maxLength={100} disabled={searching || !accountId}
        onChange={(event) => { setKeyword(event.target.value); setResults([]); setSearched(false) }}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void search() } }} />
      <Button type="button" loading={searching} disabled={searching || !accountId || !keyword.trim()} onClick={() => { void search() }}>{t('publishing.topics.search')}</Button>
    </div>
    {error ? <p role="alert" className={styles.notice}>{error}</p> : null}
    <div className={styles.topicChips} aria-label={t('publishing.topics.selected')}>
      {value.map((topic) => <Button type="button" key={topic.name} variant="ghost" disabled={disabled || searching}
        aria-label={t('publishing.topics.remove', { name: topic.name })} onClick={() => { onChange(value.filter((item) => item !== topic)) }}>
        #{topic.name}{topic.source === 'new' ? ` (${t('publishing.topics.custom')})` : ''} ×
      </Button>)}
      {!value.length ? <span className={styles.meta}>{t('publishing.topics.none')}</span> : null}
    </div>
    {searched ? <div role="status" className={styles.topicChips}>
      {results.length ? results.map(candidate) : <p>{t('publishing.topics.noResults')}</p>}
      {!results.some((topic) => topic.name === custom) && validCustom ?
        <Button type="button" disabled={disabled || value.length >= 5 || selected({ id: null, name: custom, source: 'new' })}
          onClick={() => { add({ id: null, name: custom, source: 'new' }) }}>{t('publishing.topics.addCustom', { name: custom })}</Button> : null}
    </div> : null}
    {recommendationsLoaded ? <div className={styles.recommendations}>
      <p>{t('publishing.topics.recommendations')}</p>
      <div className={styles.topicChips}>{suggestions?.length ? suggestions.map(candidate) : <span>{t('publishing.topics.noRecommendations')}</span>}</div>
    </div> : null}
  </fieldset>
}
